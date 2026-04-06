import os
import sys
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from datetime import datetime
import uvicorn

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.config import Config
from src.core.database import DatabaseManager
from src.core.logger import setup_logger
from src.agents import (
    DataCollectorAgent, SentimentAgent, TradingDecisionAgent, 
    ExecutionAgent, TelegramNotifier
)

app = FastAPI(title="CryptoTrader API", version="1.0.0")

# Global instances (initialized on startup)
config = None
logger = None
db = None
data_collector = None
sentiment = None
trading = None
executor = None
telegram = None

@app.on_event("startup")
async def startup():
    global config, logger, db, data_collector, sentiment, trading, executor, telegram
    
    config = Config.load()
    logger = setup_logger(
        'cryptotrader',
        level=config.logging.get('level', 'INFO'),
        log_file=config.logging.get('file'),
    )
    
    db = DatabaseManager(config.postgresql, logger)
    db.create_tables()
    
    sentiment = SentimentAgent(config, logger, db)
    data_collector = DataCollectorAgent(config, logger, db)
    trading = TradingDecisionAgent(config, logger, db, sentiment)
    executor = ExecutionAgent(config, logger, db)
    
    if config.telegram.get('enabled') and config.telegram.get('chat_id'):
        telegram = TelegramNotifier(
            config.telegram['bot_token'],
            config.telegram['chat_id'],
            logger
        )
    
    logger.info("CryptoTrader API started")


class ToolRequest(BaseModel):
    action: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    symbols: Optional[List[str]] = None
    exchange: Optional[str] = "binance"


class TradeRequest(BaseModel):
    symbol: str
    side: str  # BUY or SELL
    amount: str
    exchange: str = "binance"
    confirm: bool = False


# ==================== Health ====================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "agents": {
            "data_collector": data_collector is not None,
            "sentiment": sentiment is not None,
            "trading": trading is not None,
            "executor": executor is not None,
            "telegram": telegram is not None,
        }
    }


# ==================== Data Collection Tools ====================

@app.post("/tools/collect")
async def tool_collect_data(request: ToolRequest):
    """Collect market data from exchanges"""
    try:
        result = data_collector.run_once()
        return {"status": "success", "result": result}
    except Exception as e:
        if telegram:
            telegram.notify_error("DataCollector", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tools/select_symbols")
async def tool_select_symbols(request: ToolRequest):
    """Select trading symbols based on criteria"""
    try:
        symbols = data_collector.select_symbols()
        return {"status": "success", "symbols": symbols}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Sentiment Tools ====================

@app.post("/tools/sentiment")
async def tool_analyze_sentiment(request: ToolRequest):
    """Analyze news sentiment"""
    try:
        result = sentiment.run_once()
        return {"status": "success", "result": result}
    except Exception as e:
        if telegram:
            telegram.notify_error("SentimentAgent", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tools/sentiment/current")
async def tool_get_sentiment():
    """Get current aggregated sentiment"""
    try:
        agg = sentiment.get_aggregated_sentiment()
        return {"status": "success", "sentiment": agg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Trading Decision Tools ====================

@app.post("/tools/decide")
async def tool_trading_decision(request: ToolRequest):
    """Generate trading decision for symbols"""
    try:
        symbols = request.symbols or ['BTCUSDT', 'ETHUSDT']
        decisions = []
        for sym in symbols:
            result = trading.run_once_for_symbol(sym)
            decisions.append(result)
            
            # Notify via Telegram if strong signal
            if telegram and result.get('signal') != 'HOLD' and result.get('confidence', 0) > 0.7:
                telegram.notify_signal(
                    sym, result['signal'], result['confidence'], result.get('reasoning', '')
                )
        
        return {"status": "success", "decisions": decisions}
    except Exception as e:
        if telegram:
            telegram.notify_error("TradingDecision", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tools/decide/run")
async def tool_run_full_cycle():
    """Run full decision cycle for all active symbols"""
    try:
        result = trading.run_once()
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Execution Tools ====================

@app.post("/tools/execute/buy")
async def tool_execute_buy(request: TradeRequest):
    """Execute a buy order"""
    try:
        if not request.confirm:
            card = executor.generate_confirmation_card({
                'symbol': request.symbol,
                'side': 'BUY',
                'amount': request.amount,
                'exchange': request.exchange,
            })
            return {"status": "confirmation_required", "card": card}
        
        result = executor.execute_spot_buy(
            request.symbol, request.amount, request.exchange
        )
        
        if telegram and 'error' not in result:
            telegram.notify_trade(
                request.symbol, 'BUY', request.amount, 0, request.exchange, 1.0
            )
        
        return {"status": "success", "result": result}
    except Exception as e:
        if telegram:
            telegram.notify_error("Execution", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tools/execute/sell")
async def tool_execute_sell(request: TradeRequest):
    """Execute a sell order"""
    try:
        if not request.confirm:
            card = executor.generate_confirmation_card({
                'symbol': request.symbol,
                'side': 'SELL',
                'amount': request.amount,
                'exchange': request.exchange,
            })
            return {"status": "confirmation_required", "card": card}
        
        result = executor.execute_spot_sell(
            request.symbol, request.amount, request.exchange
        )
        
        if telegram and 'error' not in result:
            telegram.notify_trade(
                request.symbol, 'SELL', request.amount, 0, request.exchange, 1.0
            )
        
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Account Tools ====================

@app.get("/tools/balance")
async def tool_get_balance(exchange: str = "binance"):
    """Get account balance"""
    try:
        result = executor.get_balance(exchange)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tools/positions")
async def tool_get_positions():
    """Get all open positions"""
    try:
        positions = executor.get_all_open_positions()
        result = [{
            'id': p.id,
            'symbol': p.symbol,
            'exchange': p.exchange,
            'entry_price': float(p.entry_price),
            'quantity': float(p.quantity),
            'stop_loss': float(p.stop_loss) if p.stop_loss else None,
            'take_profit': float(p.take_profit) if p.take_profit else None,
            'unrealized_pnl': float(p.unrealized_pnl) if p.unrealized_pnl else 0,
            'unrealized_pnl_pct': float(p.unrealized_pnl_percent) if p.unrealized_pnl_percent else 0,
            'trailing_stop_activated': p.trailing_stop_activated,
            'trailing_stop_price': float(p.trailing_stop_price) if p.trailing_stop_price else None,
            'status': p.status,
            'opened_at': p.opened_at.isoformat() if p.opened_at else '',
        } for p in positions]
        return {"status": "success", "positions": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tools/ragflow/store_expert")
async def tool_store_expert(request: ToolRequest):
    """Store expert analysis in RAGFlow"""
    try:
        params = request.params or {}
        symbol = params.get('symbol', 'BTCUSDT')
        analysis = params.get('analysis', '')
        source = params.get('source', 'manual')
        
        from src.gateways import RAGFlowAPI
        ragflow_cfg = config.ragflow
        ragflow = RAGFlowAPI(
            base_url=ragflow_cfg.get('base_url', ''),
            api_key=ragflow_cfg.get('api_key', ''),
            dataset_id=ragflow_cfg.get('dataset_id'),
            logger=logger
        )
        
        result = ragflow.store_expert_analysis(symbol, analysis, source)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Data Export Tools ====================

@app.post("/tools/export")
async def tool_export_data(request: ToolRequest):
    """Export data to xlsx/csv"""
    try:
        import pandas as pd
        from pathlib import Path
        
        params = request.params or {}
        export_type = params.get('format', 'xlsx')
        data_type = params.get('data_type', 'signals')
        
        export_dir = Path(config.export.get('directory', 'data/exports'))
        export_dir.mkdir(parents=True, exist_ok=True)
        
        filename = f"{data_type}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.{export_type}"
        filepath = export_dir / filename
        
        with db.get_session() as session:
            from src.core.database import Signal, Trade, OHLCVRaw
            
            if data_type == 'signals':
                records = session.query(Signal).order_by(Signal.created_at.desc()).limit(1000).all()
                data = [{
                    'id': r.id, 'symbol': r.symbol, 'signal': r.signal_type,
                    'confidence': float(r.confidence) if r.confidence else 0,
                    'price': float(r.price) if r.price else 0,
                    'reasoning': r.reasoning, 'status': r.status,
                    'created_at': r.created_at.isoformat() if r.created_at else '',
                } for r in records]
            elif data_type == 'trades':
                records = session.query(Trade).order_by(Trade.created_at.desc()).limit(1000).all()
                data = [{
                    'id': r.id, 'symbol': r.symbol, 'side': r.side,
                    'quantity': float(r.quantity), 'price': float(r.price) if r.price else 0,
                    'pnl_percent': float(r.pnl_percent) if r.pnl_percent else 0,
                    'created_at': r.created_at.isoformat() if r.created_at else '',
                } for r in records]
            else:
                raise ValueError(f"Unknown data type: {data_type}")
        
        df = pd.DataFrame(data)
        
        if export_type == 'xlsx':
            df.to_excel(filepath, index=False)
        else:
            df.to_csv(filepath, index=False)
        
        # Log export
        with db.get_session() as session:
            from src.core.database import ExportHistory
            session.add(ExportHistory(
                export_type=export_type,
                file_path=str(filepath),
                records_count=len(data),
            ))
        
        return {
            "status": "success",
            "file": str(filepath),
            "records": len(data)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Main ====================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
