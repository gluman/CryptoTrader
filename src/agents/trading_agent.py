import pandas as pd
import numpy as np
import requests
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from .base import BaseAgent
from ..core.config import Config
from ..core.database import DatabaseManager, OHLCVRaw, Signal, Decision
from ..sentiment.sentiment_agent import SentimentAgent

class TradingDecisionAgent(BaseAgent):
    """LLM-based trading decision agent using PostgreSQL data + RAGFlow context"""
    
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
        if df.empty:
            return {}
        
        close = df['close']
        
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
        high = df['high']
        low = df['low']
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
        
        # CSS (simplified version)
        ma_period = 20
        ma = close.rolling(ma_period).mean()
        slope = (ma - ma.shift(1)) / atr if atr > 0 else 0
        css = slope.iloc[-1]
        
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
            'css_value': float(css),
            'volume_ratio': float(vol_ratio),
            'volume_sma': float(vol_sma),
            'bars_count': len(df),
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
    
    def build_prompt(self, symbol: str, indicators: Dict, sentiment: Dict,
                     recent_signals: List[Dict]) -> str:
        """Build LLM prompt for trading decision"""
        
        signal_history = "\n".join([
            f"  - {s['timestamp']}: {s['signal']} (conf={s['confidence']:.0%})"
            for s in recent_signals
        ]) or "  No recent signals"
        
        prompt = f"""You are an expert crypto trader. Analyze the data and provide a trading signal.

## Market Data for {symbol}
- Price: ${indicators.get('price', 0):,.2f}
- SMA 20: ${indicators.get('sma_20', 0):,.2f}
- SMA 50: ${indicators.get('sma_50', 0):,.2f}
- RSI 14: {indicators.get('rsi_14', 0):.1f}
- MACD: {indicators.get('macd', 0):.4f} (signal: {indicators.get('macd_signal', 0):.4f})
- ATR 14: ${indicators.get('atr_14', 0):,.2f}
- Bollinger: [{indicators.get('bb_lower', 0):,.2f} - {indicators.get('bb_upper', 0):,.2f}]
- CSS: {indicators.get('css_value', 0):.4f}
- Volume ratio: {indicators.get('volume_ratio', 0):.2f}x

## Sentiment (24h)
- Average: {sentiment.get('avg_sentiment', 0):.2f} (-1 to +1)
- Bullish ratio: {sentiment.get('bullish_ratio', 0):.0%}
- News count: {sentiment.get('news_count', 0)}

## Recent Signals
{signal_history}

## Rules
1. BUY when: CSS crosses up through 0.20, RSI < 70, bullish sentiment, price above SMA 50
2. SELL when: CSS crosses down through 0.20, RSI > 30, bearish sentiment
3. HOLD when: conflicting signals, low confidence, or waiting for confirmation
4. Do NOT buy if RSI > 70 (overbought)
5. Do NOT sell if RSI < 30 (oversold)

## Response Format (JSON only, no other text)
{{"signal": "BUY" or "SELL" or "HOLD", "confidence": 0.0-1.0, "reasoning": "brief explanation"}}"""
        
        return prompt
    
    def call_llm(self, prompt: str) -> Dict[str, Any]:
        """Call OpenRouter for decision"""
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
        
        try:
            start = datetime.utcnow()
            resp = requests.post(
                f"{self.api_base}/chat/completions",
                headers=headers, json=data, timeout=60
            )
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            
            result = resp.json()
            content = result['choices'][0]['message']['content'].strip()
            
            # Parse JSON from response
            decision = json.loads(content)
            decision['latency_ms'] = int(latency)
            decision['tokens'] = result.get('usage', {}).get('total_tokens', 0)
            
            return decision
        except Exception as e:
            self.log('error', f"LLM decision failed: {e}")
            return {
                'signal': 'HOLD',
                'confidence': 0.0,
                'reasoning': f"LLM error: {str(e)}"
            }
    
    def save_decision(self, symbol: str, exchange: str, timeframe: str,
                      indicators: Dict, sentiment: Dict, decision: Dict):
        """Save signal and decision to database"""
        with self.db.get_session() as session:
            # Create signal
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
            
            # Create decision log
            decision_log = Decision(
                signal_id=signal.id,
                timestamp=datetime.utcnow(),
                market_data_json=indicators,
                sentiment_data_json=sentiment,
                llm_model=self.model,
                decision_json=decision,
                latency_ms=decision.get('latency_ms', 0),
                total_tokens=decision.get('tokens', 0),
            )
            session.add(decision_log)
            
            return signal.id
    
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
        
        # 5. Build prompt and call LLM
        prompt = self.build_prompt(symbol, indicators, sentiment, recent)
        decision = self.call_llm(prompt)
        
        # 6. Check minimum confidence
        if decision['confidence'] < self.min_confidence:
            decision['signal'] = 'HOLD'
            decision['reasoning'] = f"Low confidence ({decision['confidence']:.0%} < {self.min_confidence:.0%})"
        
        # 7. Save to database
        signal_id = self.save_decision(symbol, exchange, timeframe, indicators, sentiment, decision)
        
        self.log('info', f"{symbol}: {decision['signal']} (conf={decision['confidence']:.0%})")
        
        return {
            'symbol': symbol,
            'signal': decision['signal'],
            'confidence': decision['confidence'],
            'reasoning': decision.get('reasoning', ''),
            'signal_id': signal_id,
            'indicators': indicators,
            'sentiment': sentiment,
        }
    
    def run_once(self) -> Dict[str, Any]:
        """Generate decisions for all active symbols"""
        self.log('info', "Starting trading decision cycle...")
        
        # Get active symbols
        from ..core.database import SelectedSymbol
        with self.db.get_session() as session:
            symbols = session.query(SelectedSymbol).filter_by(is_active=True).all()
            symbol_names = [s.symbol for s in symbols][:10]  # Limit to 10
        
        if not symbol_names:
            symbol_names = ['BTCUSDT', 'ETHUSDT']  # Default
        
        decisions = []
        for sym in symbol_names:
            try:
                result = self.run_once_for_symbol(sym)
                decisions.append(result)
            except Exception as e:
                self.log('error', f"Decision failed for {sym}: {e}")
        
        # Count signals
        buys = sum(1 for d in decisions if d.get('signal') == 'BUY')
        sells = sum(1 for d in decisions if d.get('signal') == 'SELL')
        holds = sum(1 for d in decisions if d.get('signal') == 'HOLD')
        
        self.log('info', f"Cycle complete: {buys} BUY, {sells} SELL, {holds} HOLD")
        
        return {
            'timestamp': datetime.utcnow().isoformat(),
            'decisions': decisions,
            'summary': {'buys': buys, 'sells': sells, 'holds': holds},
        }
