"""
CryptoTrader MCP Server
Provides trading tools for OpenCode agents via Model Context Protocol
"""

import sys
import os
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.config import Config
from src.core.database import DatabaseManager
from src.core.logger import setup_logger
from src.agents import (
    DataCollectorAgent, SentimentAgent, TradingDecisionAgent,
    ExecutionAgent, TelegramNotifier
)
from src.gateways import RAGFlowAPI

# Initialize components
config = Config.load()
logger = setup_logger(
    'cryptotrader.mcp',
    level=config.logging.get('level', 'INFO'),
    log_file=config.logging.get('file'),
)
db = DatabaseManager(config.postgresql, logger)

# Initialize agents
_sentiment_agent = SentimentAgent(config, logger, db)
_data_collector = DataCollectorAgent(config, logger, db)
_trading_agent = TradingDecisionAgent(config, logger, db, _sentiment_agent)
_executor = ExecutionAgent(config, logger, db)

# Telegram (optional)
_telegram = None
if config.telegram.get('enabled') and config.telegram.get('chat_id'):
    _telegram = TelegramNotifier(
        config.telegram['bot_token'],
        config.telegram['chat_id'],
        logger
    )

# RAGFlow (optional)
_ragflow = None
ragflow_cfg = config.ragflow
if ragflow_cfg.get('api_key'):
    _ragflow = RAGFlowAPI(
        base_url=ragflow_cfg.get('base_url', ''),
        api_key=ragflow_cfg.get('api_key', ''),
        dataset_id=ragflow_cfg.get('dataset_id'),
        logger=logger
    )


# ==================== Tools ====================

def collect_data(symbols: Optional[List[str]] = None, exchange: str = 'binance') -> Dict[str, Any]:
    """
    Collect market data (OHLCV) from exchanges.
    
    Args:
        symbols: List of symbols to collect (e.g., ['BTCUSDT', 'ETHUSDT']). 
                 If None, auto-selects based on volume criteria.
        exchange: Exchange to use (default: 'binance')
    
    Returns:
        Collection statistics
    """
    try:
        if symbols:
            # Collect for specific symbols
            timeframes = config.timeframes
            total_records = 0
            for symbol in symbols[:20]:
                for tf in timeframes:
                    data = _data_collector.fetch_binance_klines(symbol, tf)
                    count = _data_collector.save_ohlcv_to_db(data)
                    total_records += count
            return {
                'status': 'success',
                'symbols': symbols,
                'records': total_records,
                'timestamp': datetime.utcnow().isoformat()
            }
        else:
            # Full collection with auto-select
            result = _data_collector.run_once()
            return {'status': 'success', **result}
    except Exception as e:
        logger.error(f"collect_data failed: {e}")
        return {'status': 'error', 'error': str(e)}


def get_sentiment(hours: int = 24) -> Dict[str, Any]:
    """
    Get aggregated market sentiment from recent news.
    
    Args:
        hours: Lookback period in hours (default: 24)
    
    Returns:
        Sentiment analysis with avg score, bullish ratio, news count
    """
    try:
        result = _sentiment_agent.get_aggregated_sentiment(hours=hours)
        return {'status': 'success', 'sentiment': result}
    except Exception as e:
        logger.error(f"get_sentiment failed: {e}")
        return {'status': 'error', 'error': str(e)}


def analyze_news() -> Dict[str, Any]:
    """
    Analyze sentiment for unanalyzed news items.
    
    Returns:
        Analysis results with count of analyzed news
    """
    try:
        result = _sentiment_agent.run_once()
        return {'status': 'success', **result}
    except Exception as e:
        logger.error(f"analyze_news failed: {e}")
        return {'status': 'error', 'error': str(e)}


def make_decision(symbol: str, exchange: str = 'binance', timeframe: str = '1h') -> Dict[str, Any]:
    """
    Generate trading decision for a specific symbol using LLM + technical analysis.
    
    Args:
        symbol: Trading pair (e.g., 'BTCUSDT')
        exchange: Exchange name (default: 'binance')
        timeframe: Timeframe for analysis (default: '1h')
    
    Returns:
        Trading signal: BUY, SELL, or HOLD with confidence and reasoning
    """
    try:
        result = _trading_agent.run_once_for_symbol(symbol, exchange, timeframe)
        return {'status': 'success', **result}
    except Exception as e:
        logger.error(f"make_decision failed: {e}")
        return {'status': 'error', 'error': str(e)}


def decide_all() -> Dict[str, Any]:
    """
    Generate trading decisions for all active symbols.
    
    Returns:
        Summary of all decisions
    """
    try:
        result = _trading_agent.run_once()
        return {'status': 'success', **result}
    except Exception as e:
        logger.error(f"decide_all failed: {e}")
        return {'status': 'error', 'error': str(e)}


def execute_trades() -> Dict[str, Any]:
    """
    Execute pending trading signals (BUY/SELL orders).
    
    Returns:
        Execution results with trade details
    """
    try:
        result = _executor.run_once()
        return {'status': 'success', **result}
    except Exception as e:
        logger.error(f"execute_trades failed: {e}")
        return {'status': 'error', 'error': str(e)}


def get_balance(exchange: str = 'binance') -> Dict[str, Any]:
    """
    Get account balance from exchange.
    
    Args:
        exchange: Exchange name (default: 'binance')
    
    Returns:
        Account balances
    """
    try:
        result = _executor.get_balance(exchange)
        return {'status': 'success', 'balance': result}
    except Exception as e:
        logger.error(f"get_balance failed: {e}")
        return {'status': 'error', 'error': str(e)}


def get_positions() -> Dict[str, Any]:
    """
    Get all open trading positions.
    
    Returns:
        List of open positions with PnL, SL, TP
    """
    try:
        positions = _executor.get_all_open_positions()
        result = [{
            'id': p.id,
            'symbol': p.symbol,
            'exchange': p.exchange,
            'side': p.side,
            'entry_price': float(p.entry_price),
            'quantity': float(p.quantity),
            'cost_usdt': float(p.cost_usdt),
            'stop_loss': float(p.stop_loss) if p.stop_loss else None,
            'take_profit': float(p.take_profit) if p.take_profit else None,
            'unrealized_pnl': float(p.unrealized_pnl) if p.unrealized_pnl else 0,
            'unrealized_pnl_pct': float(p.unrealized_pnl_percent) if p.unrealized_pnl_percent else 0,
            'trailing_stop_activated': p.trailing_stop_activated,
            'trailing_stop_price': float(p.trailing_stop_price) if p.trailing_stop_price else None,
            'opened_at': p.opened_at.isoformat() if p.opened_at else '',
        } for p in positions]
        return {'status': 'success', 'positions': result}
    except Exception as e:
        logger.error(f"get_positions failed: {e}")
        return {'status': 'error', 'error': str(e)}


def close_position(symbol: str, exchange: str = 'binance', reason: str = 'MANUAL') -> Dict[str, Any]:
    """
    Close an open position for a symbol.
    
    Args:
        symbol: Trading pair (e.g., 'BTCUSDT')
        exchange: Exchange name (default: 'binance')
        reason: Reason for closing (default: 'MANUAL')
    
    Returns:
        Close result with PnL
    """
    try:
        position = _executor.get_open_position(symbol, exchange)
        if not position:
            return {'status': 'error', 'error': f'No open position for {symbol} on {exchange}'}
        
        # Get current price
        ex = _executor.exchanges.get(exchange)
        if not ex:
            return {'status': 'error', 'error': f'Exchange {exchange} not available'}
        
        ticker = ex.get_ticker(symbol)
        current_price = float(ticker.get('lastPrice', 0))
        
        if current_price <= 0:
            return {'status': 'error', 'error': 'Could not get current price'}
        
        # Execute sell
        qty = str(float(position.quantity))
        result = _executor.execute_spot_sell(symbol, qty, exchange)
        
        if 'error' in result:
            return {'status': 'error', 'error': result['error']}
        
        # Close position
        close_result = _executor.close_position(position.id, current_price, reason)
        
        return {'status': 'success', 'close': close_result, 'sell_result': result}
    except Exception as e:
        logger.error(f"close_position failed: {e}")
        return {'status': 'error', 'error': str(e)}


def get_signals(status: str = 'PENDING', limit: int = 20) -> Dict[str, Any]:
    """
    Get trading signals from database.
    
    Args:
        status: Filter by status (PENDING, EXECUTED, SKIPPED, FAILED)
        limit: Maximum number of signals to return
    
    Returns:
        List of signals
    """
    try:
        from src.core.database import Signal
        with db.get_session() as session:
            query = session.query(Signal).order_by(Signal.created_at.desc()).limit(limit)
            if status:
                query = query.filter_by(status=status)
            signals = query.all()
            
            result = [{
                'id': s.id,
                'symbol': s.symbol,
                'exchange': s.exchange,
                'timeframe': s.timeframe,
                'signal_type': s.signal_type,
                'confidence': float(s.confidence) if s.confidence else 0,
                'price': float(s.price) if s.price else 0,
                'css_value': float(s.css_value) if s.css_value else 0,
                'rsi_14': float(s.rsi_14) if s.rsi_14 else 0,
                'sentiment_score': float(s.sentiment_score) if s.sentiment_score else 0,
                'reasoning': s.reasoning or '',
                'status': s.status,
                'created_at': s.created_at.isoformat() if s.created_at else '',
            } for s in signals]
        
        return {'status': 'success', 'signals': result}
    except Exception as e:
        logger.error(f"get_signals failed: {e}")
        return {'status': 'error', 'error': str(e)}


def get_trades(limit: int = 20) -> Dict[str, Any]:
    """
    Get executed trades from database.
    
    Args:
        limit: Maximum number of trades to return
    
    Returns:
        List of executed trades
    """
    try:
        from src.core.database import Trade
        with db.get_session() as session:
            trades = session.query(Trade).order_by(Trade.created_at.desc()).limit(limit).all()
            
            result = [{
                'id': t.id,
                'symbol': t.symbol,
                'exchange': t.exchange,
                'side': t.side,
                'order_type': t.order_type,
                'quantity': float(t.quantity),
                'price': float(t.price) if t.price else 0,
                'pnl_percent': float(t.pnl_percent) if t.pnl_percent else 0,
                'pnl_absolute': float(t.pnl_absolute) if t.pnl_absolute else 0,
                'order_id': t.order_id,
                'created_at': t.created_at.isoformat() if t.created_at else '',
            } for t in trades]
        
        return {'status': 'success', 'trades': result}
    except Exception as e:
        logger.error(f"get_trades failed: {e}")
        return {'status': 'error', 'error': str(e)}


def store_expert_analysis(symbol: str, analysis: str, source: str = 'manual') -> Dict[str, Any]:
    """
    Store expert analysis in RAGFlow for future trading decisions.
    
    Args:
        symbol: Trading pair (e.g., 'BTCUSDT')
        analysis: Expert analysis text
        source: Source of analysis (default: 'manual')
    
    Returns:
        Storage result
    """
    try:
        if not _ragflow:
            return {'status': 'error', 'error': 'RAGFlow not configured'}
        
        result = _ragflow.store_expert_analysis(symbol, analysis, source)
        return {'status': 'success', 'result': result}
    except Exception as e:
        logger.error(f"store_expert_analysis failed: {e}")
        return {'status': 'error', 'error': str(e)}


def get_expert_context(symbol: str) -> Dict[str, Any]:
    """
    Get expert analysis and context from RAGFlow for a symbol.
    
    Args:
        symbol: Trading pair (e.g., 'BTCUSDT')
    
    Returns:
        Relevant expert context
    """
    try:
        if not _ragflow:
            return {'status': 'error', 'error': 'RAGFlow not configured'}
        
        context = _ragflow.get_trading_context(symbol, 'trading analysis recommendation')
        return {'status': 'success', 'context': context}
    except Exception as e:
        logger.error(f"get_expert_context failed: {e}")
        return {'status': 'error', 'error': str(e)}


def run_full_pipeline() -> Dict[str, Any]:
    """
    Run the complete trading pipeline: collect → sentiment → decide → execute.
    
    Returns:
        Full pipeline results
    """
    try:
        results = {
            'timestamp': datetime.utcnow().isoformat(),
            'steps': {},
        }
        
        # 1. Collect
        logger.info("Step 1: Collecting data...")
        results['steps']['collect'] = _data_collector.run_once()
        
        # 2. Sentiment
        logger.info("Step 2: Analyzing sentiment...")
        results['steps']['sentiment'] = _sentiment_agent.run_once()
        
        # 3. Decide
        logger.info("Step 3: Generating decisions...")
        results['steps']['decide'] = _trading_agent.run_once()
        
        # 4. Execute
        logger.info("Step 4: Executing signals...")
        results['steps']['execute'] = _executor.run_once()
        
        # 5. Telegram summary
        if _telegram:
            decide_result = results['steps']['decide']
            exec_result = results['steps']['execute']
            summary = {
                'total_signals': len(decide_result.get('decisions', [])),
                'buys': decide_result.get('summary', {}).get('buys', 0),
                'sells': decide_result.get('summary', {}).get('sells', 0),
                'holds': decide_result.get('summary', {}).get('holds', 0),
                'trades_executed': exec_result.get('executed', 0),
            }
            _telegram.notify_daily_summary(summary)
        
        return {'status': 'success', **results}
    except Exception as e:
        logger.error(f"run_full_pipeline failed: {e}")
        return {'status': 'error', 'error': str(e)}


def health_check() -> Dict[str, Any]:
    """
    Check system health: database, exchanges, RAGFlow.
    
    Returns:
        Health status of all components
    """
    try:
        health = {
            'timestamp': datetime.utcnow().isoformat(),
            'database': False,
            'exchanges': {},
            'ragflow': False,
            'telegram': False,
        }
        
        # Database
        try:
            with db.get_session() as session:
                from sqlalchemy import text
                session.execute(text('SELECT 1'))
            health['database'] = True
        except Exception:
            pass
        
        # Exchanges
        for name, ex in _executor.exchanges.items():
            try:
                ex.get_account()
                health['exchanges'][name] = 'connected'
            except Exception as e:
                health['exchanges'][name] = f'error: {str(e)[:50]}'
        
        # RAGFlow
        if _ragflow:
            try:
                _ragflow.list_datasets()
                health['ragflow'] = True
            except Exception:
                health['ragflow'] = False
        
        # Telegram
        if _telegram:
            health['telegram'] = _telegram.verify_connection()
        
        return {'status': 'success', 'health': health}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


# ==================== MCP Server ====================

if __name__ == '__main__':
    from mcp.server.fastmcp import FastMCP
    
    mcp = FastMCP(
        "CryptoTrader",
        version="1.0.0",
        description="Multi-agent crypto trading system with LLM-powered decisions"
    )
    
    # Register tools
    mcp.tool()(collect_data)
    mcp.tool()(get_sentiment)
    mcp.tool()(analyze_news)
    mcp.tool()(make_decision)
    mcp.tool()(decide_all)
    mcp.tool()(execute_trades)
    mcp.tool()(get_balance)
    mcp.tool()(get_positions)
    mcp.tool()(close_position)
    mcp.tool()(get_signals)
    mcp.tool()(get_trades)
    mcp.tool()(store_expert_analysis)
    mcp.tool()(get_expert_context)
    mcp.tool()(run_full_pipeline)
    mcp.tool()(health_check)
    
    mcp.run()
