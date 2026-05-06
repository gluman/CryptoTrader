import pandas as pd
import numpy as np
import requests
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from .base import BaseAgent
from ..core.config import Config
from sqlalchemy import text, func
from ..core.database import DatabaseManager, OHLCVRaw, Signal, Decision, Position, StrategySignal
from ..agents.sentiment_agent import SentimentAgent
from .multi_agent_engine import MultiAgentDecisionEngine


class TradingDecisionAgent(BaseAgent):
    """LLM-based trading decision agent with RAGFlow context"""
    
    # OB+BoS strategy params (backtested on BTCUSDT 4h, Feb-Apr 2026):
    # RSI+CSS as primary, OB as directional filter
    # With OB filter: 42% WR, +2.7% return, 72 trades
    # Without OB filter: 37% WR, +1.3% return, 19 trades
    BLOCK_WEIGHTS = {
        'technical': 1.0,
        'fundamental': 1.0,
        'pattern': 1.0,
        'news': 1.0,
        'market_sentiment': 1.0,
        'pump_hunter': 1.0,
        'ob_structure': 1.0,
    }
    
    # LONG: RSI<50, CSS≥0.01, ≥3 bullish blocks (incl. OB+BoS block)
    LONG_PARAMS = {
        'rsi_max': 50,
        'css_min': 0.01,
        'min_bullish_blocks': 3,
    }
    # SHORT: RSI>60, CSS≤-0.01, ≥3 bearish blocks (incl. OB+BoS block)
    SHORT_PARAMS = {
        'rsi_min': 60,
        'css_max': -0.01,
        'min_bearish_blocks': 3,
    }
    
    # Ollama fallback server
    OLLAMA_BASE = "http://192.168.0.94:11434"
    OLLAMA_MODEL = "gemma4:latest"
    OLLAMA_FALLBACK = "qwen3.5:9b"
    
    def __init__(self, config: Config, logger: logging.Logger, db: DatabaseManager, 
                 sentiment_agent: SentimentAgent):
        super().__init__('TradingDecision', logger)
        self.config = config
        self.db = db
        self.sentiment = sentiment_agent
        self.api_key = config.openrouter['api_key']
        self.api_base = config.openrouter['base_url']
        self.model = config.openrouter['model']
        self.min_confidence = config.agents.get('trading_decision', {}).get('min_confidence', 0.5)
        
        # RAGFlow disabled — no RAG context
        self.ragflow_enabled = False
        self.ragflow = None

        # Multi-Agent Engine (rule-based 6 blocks)
        self._multi_engine = MultiAgentDecisionEngine(config, logger)
    
    def get_ohlcv_data(self, symbol: str, exchange: str = 'bybit',
                       timeframe: str = '4h', limit: int = 200) -> pd.DataFrame:
        """Get OHLCV data from PostgreSQL"""
        with self.db.get_session() as session:
            # Case-insensitive: DB stores uppercase 'BYBIT', 'BINANCE', etc.
            # Try with func.lower for cross-database compatibility
            records = session.query(OHLCVRaw).filter(
                func.lower(OHLCVRaw.exchange) == exchange.lower(),
                OHLCVRaw.symbol == symbol,
                OHLCVRaw.timeframe == timeframe
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
            sl = f"${pos['stop_loss']:,.4f}" if pos.get('stop_loss') else "N/A"
            tp = f"${pos['take_profit']:,.4f}" if pos.get('take_profit') else "N/A"
            position_info = f"""
## Open Position
- Entry: ${pos['entry_price']:,.4f}
- Quantity: {pos['quantity']:.6f}
- SL: {sl}
- TP: {tp}
- Unrealized PnL: {pos['unrealized_pnl_pct']:.2f}%"""
        else:
            position_info = "\n## Open Position\n- No open position"

        # No RAG — skip rag_section
        rag_section = ""

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

## Rules
1. BUY when: CSS crosses UP through 0.20, RSI < 70, bullish sentiment, price > SMA50
2. SELL when: CSS crosses DOWN through -0.20, RSI > 30, bearish sentiment, OR when take-profit/stop-loss conditions are met
3. HOLD when: conflicting signals, low confidence, waiting for confirmation
4. Do NOT buy if RSI > 70 (overbought)
5. Do NOT sell if RSI < 30 (oversold) — wait for bounce
6. If there is an open position, consider taking profits or cutting losses based on PnL and indicators

## Response Format (JSON only, no other text)
{{"signal": "BUY" or "SELL" or "HOLD", "confidence": 0.0-1.0, "reasoning": "brief explanation"}}"""
        
        return prompt
    
    def call_llm(self, prompt: str) -> Dict[str, Any]:
        """Call LLM: DeepSeek V4 Flash -> RuAPI (claude-opus-4.6) -> Ollama

        Auto-failover on HTTP 401/402 (auth failure / insufficient balance).
        Tracks active provider in /tmp/trading_llm_active.txt for monitoring.
        """

        # === Primary: DeepSeek V4 Flash ===
        try:
            result = self._call_deepseek(prompt)
            if result:
                self._set_active_provider('deepseek')
                return result
        except Exception as e:
            err_str = str(e).lower()
            if any(code in err_str for code in ['401', '402', 'unauthorized', 'insufficient']):
                self.log('warning', f"DeepSeek auth/billing error: {e}")
            else:
                self.log('warning', f"DeepSeek failed: {e}, trying RuAPI...")

        # === Fallback 1: RuAPI (Stepanovikov) — claude-opus-4.6 ===
        try:
            result = self._call_ruapi(prompt)
            if result:
                self._set_active_provider('ruapi')
                return result
        except Exception as e:
            self.log('warning', f"RuAPI failed: {e}, trying Ollama...")

        # === Last resort: Ollama ===
        result = self._call_ollama(prompt)
        self._set_active_provider('ollama')
        return result

    def _set_active_provider(self, provider: str):
        """Write active provider to state file for monitoring."""
        try:
            state_file = '/tmp/trading_llm_active.txt'
            with open(state_file, 'w') as f:
                f.write(f"{provider}|{datetime.now().isoformat()}")
        except:
            pass

    def _call_ruapi(self, prompt: str) -> Dict[str, Any]:
        """Call RuAPI (Stepanovikov) — claude-opus-4.6."""
        import os

        # Load RuAPI key from .env
        ruapi_key = None
        env_path = os.path.expanduser('/home/andy/.env')
        if os.path.exists(env_path):
            with open(env_path, 'rb') as f:
                for line in f:
                    if line.startswith(b'RUAPI_API_KEY=') and b'***' not in line:
                        ruapi_key = line.decode('utf-8', errors='replace').strip().split('=', 1)[1]
                        break

        if not ruapi_key:
            raise ValueError("RUAPI_API_KEY not found in .env")

        headers = {
            'Authorization': f'Bearer {ruapi_key}',
            'Content-Type': 'application/json',
        }

        data = {
            'model': 'claude-opus-4.6',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.3,
            'max_tokens': 512,
        }

        start = datetime.utcnow()
        resp = requests.post(
            'https://api.stepanovikov.uno/v1/chat/completions',
            headers=headers, json=data, timeout=60
        )
        latency = (datetime.utcnow() - start).total_seconds() * 1000

        if resp.status_code == 401:
            raise ValueError(f"RuAPI HTTP 401 — Invalid API key")
        elif resp.status_code == 402:
            raise ValueError(f"RuAPI HTTP 402 — Insufficient Balance")

        result = resp.json()
        if 'choices' not in result:
            raise ValueError(f"RuAPI error: {result}")

        content = result['choices'][0]['message']['content'].strip()

        if content.startswith('```'):
            parts = content.split('```')
            if len(parts) >= 2:
                content = parts[1].strip()
                if content.lower().startswith('json'):
                    content = content[4:].strip()

        decision = json.loads(content.strip())
        decision['latency_ms'] = int(latency)
        decision['tokens'] = result.get('usage', {}).get('total_tokens', 0)
        decision['model'] = 'claude-opus-4.6 (RuAPI)'

        return decision
    
    def _call_deepseek(self, prompt: str) -> Dict[str, Any]:
        """Call DeepSeek V4 Flash API"""
        import os
        api_key = os.getenv('DEEPSEEK_API_KEY', '')
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY not set in .env")
        
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        
        data = {
            'model': 'deepseek-chat',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.3,
            'max_tokens': 512,
        }
        
        start = datetime.utcnow()
        resp = requests.post(
            'https://api.deepseek.com/chat/completions',
            headers=headers, json=data, timeout=60
        )
        latency = (datetime.utcnow() - start).total_seconds() * 1000
        
        # Check HTTP status before checking response body
        if resp.status_code in (401, 402):
            raise ValueError(f"DeepSeek HTTP {resp.status_code}: {resp.text[:200]}")

        result = resp.json()
        if 'choices' not in result:
            err_msg = str(result)
            raise ValueError(f"DeepSeek error: {err_msg}")
        content = result['choices'][0]['message']['content'].strip()
        
        # Try to parse JSON from response
        if content.startswith('```'):
            content = content.split('```')[1]
            if content.startswith('json'):
                content = content[4:]
        
        decision = json.loads(content.strip())
        decision['latency_ms'] = int(latency)
        decision['tokens'] = result.get('usage', {}).get('total_tokens', 0)
        decision['model'] = 'deepseek-chat'
        
        return decision
    
    def _call_ollama(self, prompt: str) -> Dict[str, Any]:
        """Call Ollama API with fallback to alternate model"""
        models = [self.OLLAMA_MODEL, self.OLLAMA_FALLBACK]
        
        for model in models:
            try:
                data = {
                    'model': model,
                    'prompt': prompt,
                    'temperature': self.config.openrouter.get('temperature', 0.3),
                    'max_tokens': 512,
                    'format': 'json',
                    'stream': False,
                }
                resp = requests.post(
                    f"{self.OLLAMA_BASE}/api/generate",
                    json=data,
                    timeout=120
                )
                
                result = resp.json()
                content = result.get('response', '{}').strip()
                
                # Extract JSON from response (handle markdown and extra text)
                # Remove markdown code blocks first
                if content.startswith('```'):
                    parts = content.split('```')
                    if len(parts) >= 2:
                        content = parts[1].strip()
                        if content.lower().startswith('json'):
                            content = content[4:].strip()
                
                # Find the first valid JSON object
                if not content.startswith('{'):
                    start = content.find('{')
                    if start != -1:
                        content = content[start:]
                
                # Try to decode JSON precisely
                try:
                    decision, end = json.JSONDecoder().raw_decode(content)
                    content = content[:end]
                except json.JSONDecodeError:
                    # Fallback: try extracting with regex (non-greedy)
                    import re
                    json_match = re.search(r'\{.*?\}', content, re.DOTALL)
                    if json_match:
                        content = json_match.group()
                        decision = json.loads(content)
                    else:
                        raise ValueError("No JSON object found")
                
                self.log('info', f"Ollama succeeded with model: {model}")
                decision['latency_ms'] = 0
                decision['tokens'] = 0
                decision['source'] = 'ollama'
                
                return decision
            except Exception as e:
                self.log('warning', f"Ollama model {model} failed: {e}, trying fallback...")
                continue
        
        self.log('error', "All Ollama models failed")
        return {
            'signal': 'HOLD',
            'confidence': 0.0,
            'reasoning': "All Ollama models failed"
        }
        
        self.log('error', "All Ollama models failed")
        return {
            'signal': 'HOLD',
            'confidence': 0.0,
            'reasoning': "All Ollama models failed"
        }
    
    def save_decision(self, symbol: str, exchange: str, timeframe: str,
                      indicators: Dict, sentiment: Dict, decision: Dict,
                      rag_context: str = '', market_type: str = 'spot') -> int:
        """Save signal and decision to database, return signal_id"""
        with self.db.get_session() as session:
            signal = Signal(
                symbol=symbol,
                exchange=exchange,
                market_type=market_type,
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

            # Also create StrategySignal for BUY/SELL — ExecutionAgent reads from this table
            if decision['signal'] in ('BUY', 'SELL'):
                ss = StrategySignal(
                    symbol=symbol,
                    strategy='scalping',
                    action=decision['signal'],
                    confidence=decision['confidence'],
                    entry_price=indicators.get('price'),
                    stop_loss=decision.get('stop_loss'),
                    take_profit=decision.get('take_profit'),
                    reasoning=decision.get('reasoning', ''),
                    status='pending',
                    exchange=exchange,
                )
                session.add(ss)
            
            return signal_id
    
    def run_once_for_symbol(self, symbol: str, exchange: str = 'bybit',
                            timeframe: str = '4h') -> Dict[str, Any]:
        """Generate trading decision for a single symbol"""
        
        # 1. Get market data
        df = self.get_ohlcv_data(symbol, exchange, timeframe)
        if df.empty:
            return {'symbol': symbol, 'signal': 'HOLD', 'reasoning': 'No data'}
        
        # 2. Calculate indicators
        indicators = self.calculate_indicators(df)
        
        # 3. Get sentiment (per-symbol, strip USDT/USDC suffix)
        symbol_base = symbol.replace('USDT', '').replace('USDC', '')
        sentiment = self.sentiment.get_aggregated_sentiment(hours=24, symbol=symbol_base)
        
        # 4. Get recent signals
        recent = self.get_recent_signals(symbol)
        
        # 5. Get open positions
        positions = self.get_open_positions_for_symbol(symbol)
        
        # 6. No RAG — skip context retrieval
        rag_context = ''

        # 7. Build prompt and call LLM
        prompt = self.build_prompt(symbol, indicators, sentiment, recent, positions, rag_context)
        decision = self.call_llm(prompt)
        
        # 8. Check minimum confidence
        if decision['confidence'] < self.min_confidence:
            decision['signal'] = 'HOLD'
            decision['reasoning'] = f"Low confidence ({decision['confidence']:.0%} < {self.min_confidence:.0%})"
        
        # 9. Save to database
        signal_id = self.save_decision(symbol, exchange, timeframe, indicators, sentiment, decision)

        # 10. RAGFlow disabled
        
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

    def run_once_multi_agent_for_symbol(self, symbol: str, exchange: str = 'bybit',
                                        timeframe: str = '4h') -> Dict[str, Any]:
        """Generate trading decision using 6-block Multi-Agent Engine"""
        
        # 1. Get market data from DB
        df = self.get_ohlcv_data(symbol, exchange, timeframe)
        if df.empty:
            return {'symbol': symbol, 'signal': 'HOLD', 'reasoning': 'No data', 'blocks': {}}
        
        # 3. Get sentiment (per-symbol, strip USDT/USDC suffix)
        symbol_base = symbol.replace('USDT', '').replace('USDC', '')
        sentiment = self.sentiment.get_aggregated_sentiment(hours=24, symbol=symbol_base)
        
        # 3. Get open positions
        positions = self.get_open_positions_for_symbol(symbol)
        
        # 4. Initialize Multi-Agent Engine (lazy init)
        if not hasattr(self, '_multi_engine'):
            self._multi_engine = MultiAgentDecisionEngine(self.config, self.log)
        
        # 5. Run multi-agent analysis
        decision = self._multi_engine.analyze(df, symbol, sentiment)
        
        # 6. If position exists, consider SELL
        if positions and decision['signal'] == 'BUY':
            # Don't BUY if already holding — check if should HOLD or SELL instead
            pos = positions[0]
            pnl_pct = pos.get('unrealized_pnl_pct', 0)
            if pnl_pct > 5:
                decision['signal'] = 'SELL'
                decision['reasoning'] += f' | Close existing position: PnL={pnl_pct:.1f}%'
        
        # 7. Build return compatible with old interface
        result = {
            'symbol': symbol,
            'signal': decision['signal'],
            'confidence': decision['confidence'],
            'score': decision.get('score', 0.5),
            'reasoning': decision.get('reasoning', ''),
            'blocks': decision.get('blocks', {}),
            'positions': positions,
            'sentiment': sentiment,
        }
        
        self.log('info', f"[MultiAgent] {symbol}: {decision['signal']} "
                  f"(conf={decision['confidence']:.0%}, score={decision.get('score', 0):.2f})")
        
        return result
    
    def run_once_multi_agent(self) -> Dict[str, Any]:
        """Generate decisions for all active symbols using Multi-Agent Engine"""
        self.log('info', "Starting Multi-Agent decision cycle...")
        
        with self.db.get_session() as session:
            from ..core.database import SelectedSymbol
            symbols = session.query(SelectedSymbol).filter_by(is_active=True).all()
            symbol_names = [s.symbol for s in symbols][:10]
        
        if not symbol_names:
            symbol_names = ['BTCUSDT', 'ETHUSDT']
        
        decisions = []
        for sym in symbol_names:
            try:
                result = self.run_once_multi_agent_for_symbol(sym)
                decisions.append(result)
            except Exception as e:
                self.log('error', f"MultiAgent decision failed for {sym}: {e}")
        
        buys = sum(1 for d in decisions if d.get('signal') == 'BUY')
        sells = sum(1 for d in decisions if d.get('signal') == 'SELL')
        holds = sum(1 for d in decisions if d.get('signal') == 'HOLD')
        
        self.log('info', f"Multi-Agent cycle complete: {buys} BUY, {sells} SELL, {holds} HOLD")

        return {
            'timestamp': datetime.utcnow().isoformat(),
            'decisions': decisions,
            'summary': {'buys': buys, 'sells': sells, 'holds': holds},
        }

    # ==================== LLM-Evaluated Hybrid Decision ====================

    def run_once_llm_evaluated_for_symbol(self, symbol: str, exchange: str = 'bybit',
                                          timeframe: str = '4h') -> Dict[str, Any]:
        """Hybrid: 6 rule-based blocks + LLM final decision.

        1. Get market data
        2. Run 6-block analysis → scores
        3. Call LLM with block scores + indicators → final decision
        4. Save to DB
        """

        # 1. Get market data
        df = self.get_ohlcv_data(symbol, exchange, timeframe)
        if df.empty:
            return {'symbol': symbol, 'signal': 'HOLD', 'reasoning': 'No data'}

        # 2. Calculate indicators
        indicators = self.calculate_indicators(df)

        # 3. Get sentiment (per-symbol, strip USDT/USDC suffix)
        symbol_base = symbol.replace('USDT', '').replace('USDC', '')
        sentiment = self.sentiment.get_aggregated_sentiment(hours=24, symbol=symbol_base)

        # 4. Get recent signals
        recent = self.get_recent_signals(symbol)

        # 5. Get open positions
        positions = self.get_open_positions_for_symbol(symbol)

        # 6. Run 6-block multi-agent analysis
        multi_result = self._multi_engine.analyze(df, symbol, sentiment)
        block_results = multi_result.get('blocks', {})

        # 7. Build LLM prompt with block scores
        prompt = self._build_llm_evaluated_prompt(
            symbol, indicators, sentiment, recent, positions, block_results
        )

        # 8. Call LLM
        decision = self.call_llm(prompt)

        # 9. Apply position-aware override
        if positions and decision.get('signal') == 'BUY':
            pos = positions[0]
            pnl_pct = pos.get('unrealized_pnl_pct', 0)
            if pnl_pct > 5:
                decision['signal'] = 'SELL'
                decision['reasoning'] += f' | Close existing: PnL={pnl_pct:.1f}%'

        # 10. Confidence check
        if decision['confidence'] < self.min_confidence:
            decision['signal'] = 'HOLD'
            decision['reasoning'] += f' (conf={decision["confidence"]:.0%} < {self.min_confidence:.0%})'

        # 11. Save to DB
        signal_id = self.save_decision(
            symbol, exchange, timeframe, indicators, sentiment, decision
        )

        self.log('info', f"[LLM-Eval] {symbol}: {decision['signal']} "
                  f"(conf={decision['confidence']:.0%}, blocks_used=6)")

        return {
            'symbol': symbol,
            'signal': decision['signal'],
            'confidence': decision['confidence'],
            'reasoning': decision.get('reasoning', ''),
            'signal_id': signal_id,
            'indicators': indicators,
            'sentiment': sentiment,
            'positions': positions,
            'blocks': block_results,
        }

    def run_once_rule_based_for_symbol(self, symbol: str, exchange: str = 'bybit',
                                       timeframe: str = '4h') -> Dict[str, Any]:
        """Pure rule-based decision using optimized parameters (no LLM.
        Uses LONG_PARAMS and SHORT_PARAMS thresholds found via backtesting.
        Much faster than LLM version — ~50ms per symbol.
        """
        df = self.get_ohlcv_data(symbol, exchange, timeframe)
        if df.empty:
            return {'symbol': symbol, 'signal': 'HOLD', 'reasoning': 'No data'}

        # 3. Get sentiment (per-symbol, strip USDT/USDC suffix)
        symbol_base = symbol.replace('USDT', '').replace('USDC', '')
        sentiment = self.sentiment.get_aggregated_sentiment(hours=24, symbol=symbol_base)
        positions = self.get_open_positions_for_symbol(symbol)

        multi_result = self._multi_engine.analyze(df, symbol, sentiment)
        block_results = multi_result.get('blocks', {})

        # Count bullish/bearish blocks across all 7 blocks
        bullish_blocks = sum(
            1 for r in block_results.values()
            if isinstance(r, dict) and r.get('signal') == 'BUY'
        )
        bearish_blocks = sum(
            1 for r in block_results.values()
            if isinstance(r, dict) and r.get('signal') == 'SELL'
        )

        # OB signal as directional filter (for rule-based)
        ob_result = block_results.get('ob_structure', {})
        ob_signal = ob_result.get('signal', 'HOLD') if isinstance(ob_result, dict) else 'HOLD'
        ob_conf = ob_result.get('confidence', 0.5) if isinstance(ob_result, dict) else 0.5

        ind = indicators
        lp = self.LONG_PARAMS
        sp = self.SHORT_PARAMS

        rsi = ind.get('rsi_14', 50)
        css = ind.get('css_value', 0)

        # Position override
        if positions:
            pos = positions[0]
            pnl_pct = pos.get('unrealized_pnl_pct', 0)
            if pnl_pct > 5:
                return {
                    'symbol': symbol, 'signal': 'SELL',
                    'reasoning': f'Close position: PnL={pnl_pct:.1f}% > 5%',
                    'confidence': 0.9, 'indicators': indicators,
                    'blocks': block_results, 'positions': positions,
                    'bullish_blocks': bullish_blocks, 'bearish_blocks': bearish_blocks,
                }

        # OB directional filter: skip when OB contradicts RSI+CSS direction
        # (unless extreme RSI < 35 for LONG or > 65 for SHORT)
        ob_allow_neutral_long = (rsi < 35 and ob_signal == 'HOLD')
        ob_allow_neutral_short = (rsi > 65 and ob_signal == 'HOLD')

        # HARD overrides
        if rsi > 70:
            signal = 'HOLD'
            reasoning = f'RSI={rsi:.1f}>70 (overbought) — no BUY'
            confidence = 0.8
        elif rsi < 30:
            signal = 'HOLD'
            reasoning = f'RSI={rsi:.1f}<30 (oversold) — no SELL'
            confidence = 0.8
        else:
            # Check LONG conditions
            long_cond1 = (rsi < lp['rsi_max'] and css >= lp['css_min'] and bullish_blocks >= lp['min_bullish_blocks'])
            long_cond2 = (rsi < lp['rsi_max'] and ind.get('css_cross_up', False))
            is_long = long_cond1 or long_cond2

            # Check SHORT conditions
            short_cond1 = (rsi > sp['rsi_min'] and css <= sp['css_max'] and bearish_blocks >= sp['min_bearish_blocks'])
            short_cond2 = (rsi > sp['rsi_min'] and ind.get('css_cross_down', False))
            is_short = short_cond1 or short_cond2

            # Apply OB directional filter
            # Skip LONG when OB says SELL; skip SHORT when OB says BUY
            # Allow OB=HOLD + extreme RSI as exception
            if ob_signal == 'SELL' and not ob_allow_neutral_long:
                is_long = False
            if ob_signal == 'BUY' and not ob_allow_neutral_short:
                is_short = False

            # Conflict resolution
            if is_long and is_short:
                signal = 'HOLD'
                reasoning = f'LONG+SHORT conflict (RSI={rsi:.1f}, CSS={css:.4f})'
                confidence = 0.5
            elif is_long:
                signal = 'BUY'
                reasoning = (f'LONG: RSI={rsi:.1f}<{lp["rsi_max"]}, '
                            f'CSS={css:.4f}>={lp["css_min"]}, bullish_blocks={bullish_blocks}>={lp["min_bullish_blocks"]}, '
                            f'OB={ob_signal}')
                confidence = min(0.5 + (bullish_blocks * 0.1), 0.9)
            elif is_short:
                signal = 'SELL'
                reasoning = (f'SHORT: RSI={rsi:.1f}>{sp["rsi_min"]}, '
                            f'CSS={css:.4f}<={sp["css_max"]}, bearish_blocks={bearish_blocks}>={sp["min_bearish_blocks"]}, '
                            f'OB={ob_signal}')
                confidence = min(0.5 + (bearish_blocks * 0.1), 0.9)
            else:
                signal = 'HOLD'
                reasoning = (f'No setup: RSI={rsi:.1f}, CSS={css:.4f}, '
                             f'bullish_blocks={bullish_blocks}, bearish_blocks={bearish_blocks}, OB={ob_signal}')
                confidence = 0.55

        # Save to DB
        signal_id = self.save_decision(
            symbol, exchange, timeframe, indicators, sentiment,
            {'signal': signal, 'confidence': confidence, 'reasoning': reasoning}
        )

        self.log('info', f"[RuleBased] {symbol}: {signal} conf={confidence:.0%} "
                  f"(RSI={rsi:.1f}, CSS={css:.4f}, B={bullish_blocks}, S={bearish_blocks})")

        return {
            'symbol': symbol, 'signal': signal, 'confidence': confidence,
            'reasoning': reasoning, 'signal_id': signal_id,
            'indicators': indicators, 'sentiment': sentiment,
            'positions': positions, 'blocks': block_results,
            'bullish_blocks': bullish_blocks, 'bearish_blocks': bearish_blocks,
        }

    def run_once_llm_evaluated(self) -> Dict[str, Any]:
        """Run LLM-evaluated decisions for all active symbols."""
        self.log('info', "Starting LLM-Evaluated decision cycle...")

        with self.db.get_session() as session:
            from ..core.database import SelectedSymbol
            symbols = session.query(SelectedSymbol).filter_by(is_active=True).all()
            symbol_names = [s.symbol for s in symbols][:10]

        if not symbol_names:
            symbol_names = ['BTCUSDT', 'ETHUSDT']

        decisions = []
        for sym in symbol_names:
            try:
                result = self.run_once_llm_evaluated_for_symbol(sym)
                decisions.append(result)
            except Exception as e:
                self.log('error', f"LLM-Eval decision failed for {sym}: {e}")

        buys = sum(1 for d in decisions if d.get('signal') == 'BUY')
        sells = sum(1 for d in decisions if d.get('signal') == 'SELL')
        holds = sum(1 for d in decisions if d.get('signal') == 'HOLD')

        self.log('info', f"LLM-Eval cycle complete: {buys} BUY, {sells} SELL, {holds} HOLD")

        return {
            'timestamp': datetime.utcnow().isoformat(),
            'decisions': decisions,
            'summary': {'buys': buys, 'sells': sells, 'holds': holds},
        }

    def run_once_rule_based(self) -> Dict[str, Any]:
        """Run pure rule-based decisions for all active symbols (fast, no LLM)."""
        self.log('info', "Starting Rule-Based decision cycle...")

        with self.db.get_session() as session:
            from ..core.database import SelectedSymbol
            symbols = session.query(SelectedSymbol).filter_by(is_active=True).all()
            symbol_names = [s.symbol for s in symbols][:10]

        if not symbol_names:
            symbol_names = ['BTCUSDT', 'ETHUSDT']

        decisions = []
        for sym in symbol_names:
            try:
                result = self.run_once_rule_based_for_symbol(sym, 'bybit')
                decisions.append(result)
            except Exception as e:
                self.log('error', f"Rule-Based decision failed for {sym}: {e}")

        buys = sum(1 for d in decisions if d.get('signal') == 'BUY')
        sells = sum(1 for d in decisions if d.get('signal') == 'SELL')
        holds = sum(1 for d in decisions if d.get('signal') == 'HOLD')

        self.log('info', f"Rule-Based cycle complete: {buys} BUY, {sells} SELL, {holds} HOLD")

        return {
            'timestamp': datetime.utcnow().isoformat(),
            'decisions': decisions,
            'summary': {'buys': buys, 'sells': sells, 'holds': holds},
        }

    def _build_llm_evaluated_prompt(self, symbol: str, indicators: Dict,
                                    sentiment: Dict, recent_signals: List[Dict],
                                    positions: List[Dict],
                                    block_results: Dict) -> str:
        """Build prompt for LLM final decision using 6-block scores."""

        signal_history = "\n".join([
            f"  - {s['timestamp']}: {s['signal']} (conf={s['confidence']:.0%})"
            for s in recent_signals
        ]) or "  No recent signals"

        # Build position info for prompt
        position_info = ""
        if positions:
            pos = positions[0]
            sl = f"${pos['stop_loss']:,.4f}" if pos.get('stop_loss') else "N/A"
            tp = f"${pos['take_profit']:,.4f}" if pos.get('take_profit') else "N/A"
            position_info = f"""
## Open Position
- Entry: ${pos['entry_price']:,.4f}
- Quantity: {pos['quantity']:.6f}
- SL: {sl}
- TP: {tp}
- Unrealized PnL: {pos['unrealized_pnl_pct']:.2f}%"""
        else:
            position_info = "\n## Open Position\n- No open position"

        # Block scores summary
        block_lines = []
        bullish_blocks = 0
        bearish_blocks = 0
        for block_name, result in block_results.items():
            # Defensive: skip if result is not a dict
            if not isinstance(result, dict):
                self.log('warning', f"Block {block_name} returned non-dict: {type(result).__name__}")
                continue
            score = result.get('score', 0.5)
            sig = result.get('signal', 'HOLD')
            reasoning = result.get('reasoning', '')
            block_lines.append(f"- {block_name.upper()}: {sig} (score={score:.2f}) — {reasoning}")
            if sig == 'BUY':
                bullish_blocks += 1
            elif sig == 'SELL':
                bearish_blocks += 1
        blocks_text = "\n".join(block_lines)
        block_bias = 'BULLISH' if bullish_blocks > bearish_blocks else 'BEARISH' if bearish_blocks > bullish_blocks else 'NEUTRAL'

        # Inject optimized parameters into prompt
        lp = self.LONG_PARAMS
        sp = self.SHORT_PARAMS

        prompt = f"""You are an expert crypto trader. You receive data from 6 independent analytical blocks, then make the final trading decision.

## Symbol: {symbol}

## Market Indicators
- Price: ${indicators.get('price', 0):,.4f}
- SMA 20: ${indicators.get('sma_20', 0):,.4f}
- SMA 50: ${indicators.get('sma_50', 0):,.4f}
- RSI 14: {indicators.get('rsi_14', 0):.1f}
- MACD hist: {indicators.get('macd_hist', 0):.6f}
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

## 6-Block Analysis (rule-based)
{blocks_text}

## Block Summary
- BUY signals: {bullish_blocks}/6
- SELL signals: {bearish_blocks}/6
- Initial bias from blocks: {block_bias}

## Decision Rules (optimized from backtesting — use these hard rules)
### LONG (BUY) conditions — ALL must be true:
1. RSI < {lp['rsi_max']} (current: {{indicators.get('rsi_14', 0):.1f}})
2. CSS ≥ {lp['css_min']} (current: {{indicators.get('css_value', 0):.4f}})
3. Bullish blocks ≥ {lp['min_bullish_blocks']} (current: {{bullish_blocks}})
4. OR: RSI < {lp['rsi_max']} AND CSS just crossed UP → BUY signal

### SHORT (SELL) conditions — ALL must be true:
1. RSI > {sp['rsi_min']} (current: {{indicators.get('rsi_14', 0):.1f}})
2. CSS ≤ {sp['css_max']} (current: {{indicators.get('css_value', 0):.4f}})
3. Bearish blocks ≥ {sp['min_bearish_blocks']} (current: {{bearish_blocks}})
4. OR: RSI > {sp['rsi_min']} AND CSS just crossed DOWN → SELL signal

### HARD OVERRIDES:
- NEVER BUY if RSI > 70 (overbought)
- NEVER SELL if RSI < 30 (oversold)
- HOLD if LONG and SHORT conditions both trigger (conflict)
- If position exists: PnL > 5% → close regardless of new signals

## Response Format (JSON only)
{{"signal": "BUY" or "SELL" or "HOLD", "confidence": 0.0-1.0, "reasoning": "brief explanation"}}"""

        return prompt
