import pandas as pd
import numpy as np
import requests
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from .base import BaseAgent
from ..core.config import Config
from ..core.database import DatabaseManager, OHLCVRaw, Signal, Decision, Position
from ..agents.sentiment_agent import SentimentAgent
from ..gateways import RAGFlowAPI


class TradingDecisionAgent(BaseAgent):
    """LLM-based trading decision agent with RAGFlow context"""
    
    # Ollama fallback server
    OLLAMA_BASE = "http://192.168.0.94:11434"
    OLLAMA_MODEL = "qwen3.5:9b"
    
    def __init__(self, config: Config, logger: logging.Logger, db: DatabaseManager, 
                 sentiment_agent: SentimentAgent):
        super().__init__('TradingDecision', logger)
        self.config = config
        self.db = db
        self.sentiment = sentiment_agent
        self.api_key = config.openrouter['api_key']
        self.api_base = config.openrouter['base_url']
        self.model = config.openrouter['model']
        self.min_confidence = config.agents.get('trading_decision', {}).get('min_confidence', 0.6)
        
        # Initialize RAGFlow
        ragflow_cfg = config.ragflow
        self.ragflow = RAGFlowAPI(
            base_url=ragflow_cfg.get('base_url', ''),
            api_key=ragflow_cfg.get('api_key', ''),
            dataset_id=ragflow_cfg.get('dataset_id'),
            logger=logger
        )
        self.ragflow_enabled = bool(ragflow_cfg.get('api_key'))
    
    def get_ohlcv_data(self, symbol: str, exchange: str = 'binance', 
                       timeframe: str = '1h', limit: int = 200) -> pd.DataFrame:
        """Get OHLCV data from PostgreSQL"""
        with self.db.get_session() as session:
            records = session.query(OHLCVRaw).filter_by(
                exchange=exchange, symbol=symbol, timeframe=timeframe
            ).order_by(OHLCVRaw.timestamp.desc()).limit(limit).all()
            
            if not records:
                return pd.DataFrame()
            
            data = [{
                'timestamp': r.timestamp,
                'open': float(r.open),
                'high': float(r.high),
                'low': float(r.low),
                'close': float(r.close),
                'volume': float(r.volume),
            } for r in reversed(records)]
            
            return pd.DataFrame(data).set_index('timestamp')
    
    def calculate_indicators(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Calculate technical indicators"""
        if df.empty or len(df) < 50:
            return {}
        
        close = df['close']
        high = df['high']
        low = df['low']
        
        # SMA
        sma_20 = close.rolling(20).mean().iloc[-1]
        sma_50 = close.rolling(50).mean().iloc[-1]
        
        # RSI
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = rsi.iloc[-1]
        
        # MACD
        ema_12 = close.ewm(span=12).mean()
        ema_26 = close.ewm(span=26).mean()
        macd = ema_12 - ema_26
        macd_signal = macd.ewm(span=9).mean()
        macd_hist = macd - macd_signal
        
        # ATR
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        
        # Bollinger Bands
        bb_middle = sma_20
        bb_std = close.rolling(20).std().iloc[-1]
        bb_upper = bb_middle + (2 * bb_std)
        bb_lower = bb_middle - (2 * bb_std)
        
        # Volume analysis
        vol_sma = df['volume'].rolling(20).mean().iloc[-1]
        vol_ratio = df['volume'].iloc[-1] / vol_sma if vol_sma > 0 else 1.0
        
        # CSS (Currency Slope Strength) — full implementation
        css_result = self._calculate_css(close, atr)
        
        return {
            'price': float(close.iloc[-1]),
            'sma_20': float(sma_20),
            'sma_50': float(sma_50),
            'rsi_14': float(rsi_val),
            'macd': float(macd.iloc[-1]),
            'macd_signal': float(macd_signal.iloc[-1]),
            'macd_hist': float(macd_hist.iloc[-1]),
            'atr_14': float(atr),
            'bb_upper': float(bb_upper),
            'bb_lower': float(bb_lower),
            'bb_middle': float(bb_middle),
            'css_value': css_result['css'],
            'css_prior': css_result['css_prior'],
            'css_trend': css_result['trend'],
            'css_cross_up': css_result['cross_up'],
            'css_cross_down': css_result['cross_down'],
            'volume_ratio': float(vol_ratio),
            'volume_sma': float(vol_sma),
            'bars_count': len(df),
        }
    
    def _calculate_css(self, close: pd.Series, atr: float) -> Dict[str, Any]:
        """Calculate Currency Slope Strength (CSS)
        
        CSS = normalized MA slope / ATR
        Measures trend strength normalized by volatility
        """
        ma_period = self.config.css_indicator.get('sma_period', 20)
        
        # Calculate moving average
        ma = close.rolling(ma_period).mean()
        
        # Calculate slope (difference from previous bar)
        if atr > 0:
            slope = (ma - ma.shift(1)) / atr
        else:
            slope = ma - ma.shift(1)
        
        # Normalize to -1..+1 range
        css_current = float(slope.iloc[-1])
        css_prior = float(slope.iloc[-2]) if len(slope) > 1 else 0.0
        
        # Determine trend direction
        if css_current > 0.05:
            trend = 'BULLISH'
        elif css_current < -0.05:
            trend = 'BEARISH'
        else:
            trend = 'NEUTRAL'
        
        # Detect crossovers through the trade level
        level = self.config.css_indicator.get('level_trade', 0.20)
        cross_up = css_prior < level and css_current >= level
        cross_down = css_prior > -level and css_current <= -level
        
        return {
            'css': round(css_current, 6),
            'css_prior': round(css_prior, 6),
            'trend': trend,
            'cross_up': cross_up,
            'cross_down': cross_down,
        }
    
    def get_recent_signals(self, symbol: str, hours: int = 24) -> List[Dict]:
        """Get recent signals for a symbol"""
        since = datetime.utcnow() - timedelta(hours=hours)
        
        with self.db.get_session() as session:
            signals = session.query(Signal).filter(
                Signal.symbol == symbol,
                Signal.timestamp >= since
            ).order_by(Signal.timestamp.desc()).limit(5).all()
            
            return [{
                'timestamp': s.timestamp.isoformat(),
                'signal': s.signal_type,
                'confidence': float(s.confidence) if s.confidence else 0,
                'reasoning': s.reasoning or '',
            } for s in signals]
    
    def get_open_positions_for_symbol(self, symbol: str) -> List[Dict]:
        """Get open positions for a symbol"""
        with self.db.get_session() as session:
            positions = session.query(Position).filter_by(
                symbol=symbol, status='OPEN'
            ).all()
            
            return [{
                'id': p.id,
                'entry_price': float(p.entry_price),
                'quantity': float(p.quantity),
                'stop_loss': float(p.stop_loss) if p.stop_loss else None,
                'take_profit': float(p.take_profit) if p.take_profit else None,
                'unrealized_pnl_pct': float(p.unrealized_pnl_percent) if p.unrealized_pnl_percent else 0,
                'opened_at': p.opened_at.isoformat() if p.opened_at else '',
            } for p in positions]
    
    def build_prompt(self, symbol: str, indicators: Dict, sentiment: Dict,
                     recent_signals: List[Dict], positions: List[Dict],
                     rag_context: str = '') -> str:
        """Build LLM prompt for trading decision with RAG context"""
        
        signal_history = "\n".join([
            f"  - {s['timestamp']}: {s['signal']} (conf={s['confidence']:.0%})"
            for s in recent_signals
        ]) or "  No recent signals"
        
        position_info = ""
        if positions:
            pos = positions[0]
            position_info = f"""
## Open Position
- Entry: ${pos['entry_price']:,.4f}
- Quantity: {pos['quantity']:.6f}
- SL: ${pos['stop_loss']:,.4f if pos['stop_loss'] else 0}
- TP: ${pos['take_profit']:,.4f if pos['take_profit'] else 0}
- Unrealized PnL: {pos['unrealized_pnl_pct']:.2f}%"""
        else:
            position_info = "\n## Open Position\n- No open position"
        
        rag_section = ""
        if rag_context:
            rag_section = f"""
## Expert Knowledge & Context (from RAG)
{rag_context[:2000]}"""
        
        prompt = f"""You are an expert crypto trader. Analyze the data and provide a trading signal.

## Market Data for {symbol}
- Price: ${indicators.get('price', 0):,.4f}
- SMA 20: ${indicators.get('sma_20', 0):,.4f}
- SMA 50: ${indicators.get('sma_50', 0):,.4f}
- RSI 14: {indicators.get('rsi_14', 0):.1f}
- MACD: {indicators.get('macd', 0):.6f} (signal: {indicators.get('macd_signal', 0):.6f}, hist: {indicators.get('macd_hist', 0):.6f})
- ATR 14: ${indicators.get('atr_14', 0):,.4f}
- Bollinger: [{indicators.get('bb_lower', 0):,.4f} - {indicators.get('bb_upper', 0):,.4f}]
- CSS: {indicators.get('css_value', 0):.6f} (trend: {indicators.get('css_trend', 'N/A')})
- CSS cross up: {indicators.get('css_cross_up', False)}, cross down: {indicators.get('css_cross_down', False)}
- Volume ratio: {indicators.get('volume_ratio', 0):.2f}x

## Sentiment (24h)
- Average: {sentiment.get('avg_sentiment', 0):.2f} (-1 to +1)
- Bullish ratio: {sentiment.get('bullish_ratio', 0):.0%}
- News count: {sentiment.get('news_count', 0)}

## Recent Signals
{signal_history}
{position_info}
{rag_section}

## Rules
1. BUY when: CSS crosses UP through 0.20, RSI < 70, bullish sentiment, price > SMA50
2. SELL when: CSS crosses DOWN through -0.20, RSI > 30, bearish sentiment, OR when take-profit/stop-loss conditions are met
3. HOLD when: conflicting signals, low confidence, waiting for confirmation
4. Do NOT buy if RSI > 70 (overbought)
5. Do NOT sell if RSI < 30 (oversold) — wait for bounce
6. If there is an open position, consider taking profits or cutting losses based on PnL and indicators
7. Use expert knowledge from RAG context if relevant

## Response Format (JSON only, no other text)
{{"signal": "BUY" or "SELL" or "HOLD", "confidence": 0.0-1.0, "reasoning": "brief explanation"}}"""
        
        return prompt
    
    def call_llm(self, prompt: str) -> Dict[str, Any]:
        """Call OpenRouter for decision with Ollama fallback"""
        
        # Try OpenRouter first
        try:
            result = self._call_openrouter(prompt)
            if result:
                return result
        except Exception as e:
            self.log('warning', f"OpenRouter failed: {e}, trying Ollama...")
        
        # Fallback to Ollama
        return self._call_ollama(prompt)
    
    def _call_openrouter(self, prompt: str) -> Dict[str, Any]:
        """Call OpenRouter API"""
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        
        data = {
            'model': self.model,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': self.config.openrouter.get('temperature', 0.3),
            'max_tokens': self.config.openrouter.get('max_tokens', 512),
        }
        
        start = datetime.utcnow()
        resp = requests.post(
            f"{self.api_base}/chat/completions",
            headers=headers, json=data, timeout=60
        )
        latency = (datetime.utcnow() - start).total_seconds() * 1000
        
        result = resp.json()
        content = result['choices'][0]['message']['content'].strip()
        
        # Try to parse JSON from response
        if content.startswith('```'):
            content = content.split('```')[1]
            if content.startswith('json'):
                content = content[4:]
        
        decision = json.loads(content.strip())
        decision['latency_ms'] = int(latency)
        decision['tokens'] = result.get('usage', {}).get('total_tokens', 0)
        
        return decision
    
    def _call_ollama(self, prompt: str) -> Dict[str, Any]:
        """Call Ollama API as fallback"""
        try:
            data = {
                'model': self.OLLAMA_MODEL,
                'prompt': prompt,
                'temperature': self.config.openrouter.get('temperature', 0.3),
                'max_tokens': 512,
                'format': 'json',
            }
            resp = requests.post(
                f"{self.OLLAMA_BASE}/api/generate",
                json=data,
                timeout=120
            )
            
            result = resp.json()
            content = result.get('response', '{}').strip()
            
            # Parse JSON
            if content.startswith('```'):
                content = content.split('```')[1]
                if content.startswith('json'):
                    content = content[4:]
            
            decision = json.loads(content.strip())
            decision['latency_ms'] = 0
            decision['tokens'] = 0
            decision['source'] = 'ollama'
            
            return decision
        except Exception as e:
            self.log('error', f"Ollama call failed: {e}")
            return {
                'signal': 'HOLD',
                'confidence': 0.0,
                'reasoning': f"LLM error: {str(e)}"
            }
    
    def save_decision(self, symbol: str, exchange: str, timeframe: str,
                      indicators: Dict, sentiment: Dict, decision: Dict,
                      rag_context: str = '') -> int:
        """Save signal and decision to database, return signal_id"""
        with self.db.get_session() as session:
            signal = Signal(
                symbol=symbol,
                exchange=exchange,
                timeframe=timeframe,
                timestamp=datetime.utcnow(),
                signal_type=decision['signal'],
                strength=decision['confidence'],
                css_value=indicators.get('css_value'),
                rsi_14=indicators.get('rsi_14'),
                macd=indicators.get('macd'),
                atr_14=indicators.get('atr_14'),
                price=indicators.get('price'),
                sentiment_score=sentiment.get('avg_sentiment'),
                news_volume=sentiment.get('news_count', 0),
                volume_24h=indicators.get('volume_sma'),
                confidence=decision['confidence'],
                model_version='openrouter_v1',
                reasoning=decision.get('reasoning', ''),
                status='PENDING',
            )
            session.add(signal)
            session.flush()
            signal_id = signal.id
            
            decision_log = Decision(
                signal_id=signal_id,
                timestamp=datetime.utcnow(),
                market_data_json=indicators,
                sentiment_data_json=sentiment,
                news_context=rag_context[:2000] if rag_context else None,
                llm_model=self.model,
                decision_json=decision,
                latency_ms=decision.get('latency_ms', 0),
                total_tokens=decision.get('tokens', 0),
            )
            session.add(decision_log)
            
            return signal_id
    
    def run_once_for_symbol(self, symbol: str, exchange: str = 'binance',
                            timeframe: str = '1h') -> Dict[str, Any]:
        """Generate trading decision for a single symbol"""
        
        # 1. Get market data
        df = self.get_ohlcv_data(symbol, exchange, timeframe)
        if df.empty:
            return {'symbol': symbol, 'signal': 'HOLD', 'reasoning': 'No data'}
        
        # 2. Calculate indicators
        indicators = self.calculate_indicators(df)
        
        # 3. Get sentiment
        sentiment = self.sentiment.get_aggregated_sentiment(hours=24)
        
        # 4. Get recent signals
        recent = self.get_recent_signals(symbol)
        
        # 5. Get open positions
        positions = self.get_open_positions_for_symbol(symbol)
        
        # 6. Get RAG context
        rag_context = ''
        if self.ragflow_enabled:
            try:
                rag_context = self.ragflow.get_trading_context(
                    symbol, 
                    f"trading analysis {symbol} buy sell decision"
                )
                self.log('debug', f"RAG context length: {len(rag_context)} chars")
            except Exception as e:
                self.log('warning', f"RAGFlow retrieval failed: {e}")
        
        # 7. Build prompt and call LLM
        prompt = self.build_prompt(symbol, indicators, sentiment, recent, positions, rag_context)
        decision = self.call_llm(prompt)
        
        # 8. Check minimum confidence
        if decision['confidence'] < self.min_confidence:
            decision['signal'] = 'HOLD'
            decision['reasoning'] = f"Low confidence ({decision['confidence']:.0%} < {self.min_confidence:.0%})"
        
        # 9. Save to database
        signal_id = self.save_decision(symbol, exchange, timeframe, indicators, sentiment, decision, rag_context)
        
        # 10. Store decision in RAGFlow for future reference
        if self.ragflow_enabled and decision['signal'] != 'HOLD':
            try:
                self.ragflow.store_trading_journal(
                    symbol=symbol,
                    action=decision['signal'],
                    price=indicators.get('price', 0),
                    reasoning=decision.get('reasoning', ''),
                )
            except Exception as e:
                self.log('warning', f"Failed to store journal in RAGFlow: {e}")
        
        self.log('info', f"{symbol}: {decision['signal']} (conf={decision['confidence']:.0%})")
        
        return {
            'symbol': symbol,
            'signal': decision['signal'],
            'confidence': decision['confidence'],
            'reasoning': decision.get('reasoning', ''),
            'signal_id': signal_id,
            'indicators': indicators,
            'sentiment': sentiment,
            'positions': positions,
            'rag_context_length': len(rag_context),
        }
    
    def run_once(self) -> Dict[str, Any]:
        """Generate decisions for all active symbols"""
        self.log('info', "Starting trading decision cycle...")
        
        with self.db.get_session() as session:
            from ..core.database import SelectedSymbol
            symbols = session.query(SelectedSymbol).filter_by(is_active=True).all()
            symbol_names = [s.symbol for s in symbols][:10]
        
        if not symbol_names:
            symbol_names = ['BTCUSDT', 'ETHUSDT']
        
        decisions = []
        for sym in symbol_names:
            try:
                result = self.run_once_for_symbol(sym)
                decisions.append(result)
            except Exception as e:
                self.log('error', f"Decision failed for {sym}: {e}")
        
        buys = sum(1 for d in decisions if d.get('signal') == 'BUY')
        sells = sum(1 for d in decisions if d.get('signal') == 'SELL')
        holds = sum(1 for d in decisions if d.get('signal') == 'HOLD')
        
        self.log('info', f"Cycle complete: {buys} BUY, {sells} SELL, {holds} HOLD")
        
        return {
            'timestamp': datetime.utcnow().isoformat(),
            'decisions': decisions,
            'summary': {'buys': buys, 'sells': sells, 'holds': holds},
        }
