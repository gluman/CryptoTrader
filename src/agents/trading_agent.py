import os
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
    OLLAMA_MODEL = "gemma4:e2b"
    OLLAMA_FALLBACK = "qwen3.5:9b"
    
    def __init__(self, config: Config, logger: logging.Logger, db: DatabaseManager, 
                 sentiment_agent: SentimentAgent):
        super().__init__('TradingDecision', logger)
        self.config = config
        self.db = db
        self.sentiment = sentiment_agent
        # DeepSeek API (primary) — uses DEEPSEEK_API_KEY from env
        self.deepseek_key = os.environ.get('DEEPSEEK_API_KEY', '')
        self.deepseek_base = 'https://api.deepseek.com/v1'
        self.deepseek_model = 'deepseek-chat'  # V4 Flash via chat completions
        self.model = 'deepseek-chat'  # Used in save_decision fallback
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

        # FDI (Fractal Dimension Index) — regime detection
        fdi_result = self._calculate_fdi(close)

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
            'fdi': fdi_result['fdi'],
            'regime': fdi_result['regime'],  # 'trending' or 'ranging'
        }
    
    def _calculate_css(self, close: pd.Series, atr: float) -> Dict[str, Any]:
        """Calculate Currency Slope Strength (CSS)
        
        CSS = z-score normalized MA slope / ATR
        Measures trend strength normalized by volatility and recent range.
        Z-score normalization ensures thresholds are meaningful regardless of asset volatility.
        """
        ma_period = self.config.css_indicator.get('sma_period', 20)
        z_score_period = self.config.css_indicator.get('z_score_period', 50)
        
        # Calculate moving average
        ma = close.rolling(ma_period).mean()
        
        # Calculate slope (difference from previous bar)
        if atr > 0:
            slope = (ma - ma.shift(1)) / atr
        else:
            slope = ma - ma.shift(1)
        
        # Z-score normalization over lookback period
        slope_mean = slope.rolling(z_score_period).mean()
        slope_std = slope.rolling(z_score_period).std()
        css_z = (slope - slope_mean) / (slope_std + 1e-10)
        
        # CSS is the z-score of the slope
        css_current = float(css_z.iloc[-1])
        css_prior = float(css_z.iloc[-2]) if len(css_z) > 1 else 0.0
        
        # Determine trend direction using z-score thresholds
        if css_current > 1.5:
            trend = 'BULLISH'
        elif css_current < -1.5:
            trend = 'BEARISH'
        else:
            trend = 'NEUTRAL'
        
        # Detect crossovers through the trade level (z-score = +/-1.0)
        level = self.config.css_indicator.get('level_trade', 1.0)
        cross_up = css_prior < level and css_current >= level
        cross_down = css_prior > -level and css_current <= -level
        
        return {
            'css': round(css_current, 6),
            'css_prior': round(css_prior, 6),
            'trend': trend,
            'cross_up': cross_up,
            'cross_down': cross_down,
        }

    def _calculate_fdi(self, close: pd.Series, period: int = 50) -> Dict[str, Any]:
        """Calculate Fractal Dimension Index (FDI)
        
        FDI < 0.5 → Trending market → momentum trades (breakouts)
        FDI >= 0.5 → Ranging market → mean reversion trades (fractal fades)
        
        Standard FDI formula: FDI = 2 - D, where D = log(n) / log(n + sum_of_ranges)
        - Trending markets have low FDI (close to 1.0)
        - Ranging markets have higher FDI (closer to 2.0)
        """
        if len(close) < period:
            return {'fdi': 0.5, 'regime': 'unknown'}
        
        # Calculate the sum of price ranges over the period
        # sum_of_ranges = sum of |close[i] - close[i-1]| for i in 1..period
        ranges = np.abs(close - close.shift(1)).rolling(period).sum()
        
        # Fractal dimension: D = log(n) / log(n + sum_of_ranges)
        n = float(period)
        fd = np.log(n) / np.log(n + ranges)
        fdi = 2 - fd
        
        fdi_val = float(fdi.iloc[-1].clip(0, 2))
        regime = 'trending' if fdi_val < 1.5 else 'ranging'
        
        return {
            'fdi': round(fdi_val, 4),
            'regime': regime,
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
            sl_str = f"{pos['stop_loss']:,.4f}" if pos['stop_loss'] else "none"
            tp_str = f"{pos['take_profit']:,.4f}" if pos['take_profit'] else "none"
            position_info = f"""
## Open Position
- Entry: ${pos['entry_price']:,.4f}
- Quantity: {pos['quantity']:.6f}
- SL: ${sl_str}
- TP: ${tp_str}
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
- FDI: {indicators.get('fdi', 0.5):.4f} (regime: {indicators.get('regime', 'unknown')})

## Market Regime
- If FDI < 0.5 (trending): Look for momentum breakouts. BUY on bullish breakouts, SELL on bearish breakouts.
- If FDI >= 0.5 (ranging): Mean reversion mode. BUY at support bounces, SELL at resistance rejections.

## Sentiment (24h)
- Average: {sentiment.get('avg_sentiment', 0):.2f} (-1 to +1)
- Bullish ratio: {sentiment.get('bullish_ratio', 0):.0%}
- News count: {sentiment.get('news_count', 0)}

## Recent Signals
{signal_history}
{position_info}
{rag_section}

## Rules
1. In TRENDING markets (FDI < 0.5): Follow the trend. BUY on CSS cross UP + breakout, SELL on CSS cross DOWN + breakdown.
2. In RANGING markets (FDI >= 0.5): Fade the moves. BUY at lower Bollinger Band bounces, SELL at upper Bollinger Band rejections.
3. HOLD when: conflicting signals, low confidence, or regime unclear.
4. Do NOT buy if RSI > 75 (overbought in any regime)
5. Do NOT sell if RSI < 25 (oversold in any regime)
6. If there is an open position, YOU MUST decide: hold (HOLD), take profit, or cut losses (SELL) based on PnL and indicators
7. Use expert knowledge from RAG context if relevant
8. For BUY signals: calculate stop_loss and take_profit prices based on ATR and market structure
9. For SELL signals: stop_loss/take_profit are ignored (position closes entirely)
10. Do NOT open a new position if there is already an open position for this symbol

## SL/TP MANDATORY RULES (must follow):
- STOP LOSS (BUY): MUST be 1.5% to 3.0% BELOW entry price. Never closer than 1.5%!
- STOP LOSS (SELL): MUST be 1.5% to 3.0% ABOVE entry price. Never closer than 1.5%!
- TAKE PROFIT (BUY): MUST be at least 3% ABOVE entry price
- TAKE PROFIT (SELL): MUST be at least 3% BELOW entry price
- RISK/REWARD: TP distance must be at least 1.5x the SL distance (RR >= 1.5)
- If you cannot set SL at 1.5%+ and TP at 3%+ with RR >= 1.5 → respond HOLD instead

## Response Format (JSON only, no other text)
{{"signal": "BUY" or "SELL" or "HOLD", "confidence": 0.0-1.0, "reasoning": "brief explanation", "stop_loss": price or null, "take_profit": price or null}}

Example for BTC @ 68000:
  BUY with SL=66300 (2.5% below), TP=72000 (5.9% above) → RR=2.4:1 ✅
  Or: HOLD if market is uncertain"""
        
        return prompt
    
    def call_llm(self, prompt: str) -> Dict[str, Any]:
        """DeepSeek (primary) → Ollama (local) → rule-based (final fallback)"""
        # 1. DeepSeek (paid, best quality)
        try:
            decision = self._call_deepseek(prompt)
            self.log('info', f"DeepSeek decision: {decision.get('signal')} (conf={decision.get('confidence', 0):.0%})")
            return decision
        except Exception as e:
            self.log('warning', f"DeepSeek call failed ({e}), trying Ollama")

        # 2. Ollama (local, free)
        try:
            decision = self._call_ollama(prompt)
            if decision.get('signal') and decision.get('reasoning') != 'All Ollama models failed':
                self.log('info', f"Ollama decision: {decision.get('signal')} (conf={decision.get('confidence', 0):.0%})")
                return decision
        except Exception as e:
            self.log('warning', f"Ollama call failed ({e}), trying rule-based")

        # 3. Rule-based (last resort)
        return self._rule_based_decision(prompt)

    def _rule_based_decision(self, prompt: str) -> Dict[str, Any]:
        """Simple rule-based decision without LLM for reliability"""
        
        indicators = self._parse_indicators_from_prompt(prompt)
        
        signal = 'HOLD'
        confidence = 0.0
        reasoning = ''
        
        css = indicators.get('css_value', 0)
        rsi = indicators.get('rsi_14', 50)
        price = indicators.get('price', 0)
        sma20 = indicators.get('sma_20', 0)
        sma50 = indicators.get('sma_50', 0)
        macd_hist = indicators.get('macd_hist', 0)
        
        if indicators.get('css_cross_up') and rsi < 70 and price > sma50:
            signal = 'BUY'
            confidence = 0.75
            reasoning = f"CSS cross up ({css:.4f}), RSI {rsi:.1f}, price above SMA50"
        elif indicators.get('css_cross_down') and rsi > 30:
            signal = 'SELL'
            confidence = 0.75
            reasoning = f"CSS cross down ({css:.4f}), RSI {rsi:.1f}"
        elif css > 0.15 and rsi < 60 and price > sma20:
            signal = 'BUY'
            confidence = 0.65
            reasoning = f"Strong CSS ({css:.4f}), favorable RSI"
        elif css < -0.15 and rsi > 40:
            signal = 'SELL'
            confidence = 0.65
            reasoning = f"Weak CSS ({css:.4f}), bearish"
        
        self.log('info', f"Rule-based decision: {signal} (conf={confidence:.0%})")
        
        return {
            'signal': signal,
            'confidence': confidence,
            'reasoning': reasoning,
            'source': 'rule_based'
        }
    
    def _parse_indicators_from_prompt(self, prompt: str) -> Dict:
        """Extract indicators from prompt for rule-based decisions"""
        import re
        indicators = {}
        
        patterns = {
            'price': r'Price: \$?([0-9.]+)',
            'sma_20': r'SMA 20: \$?([0-9.]+)',
            'sma_50': r'SMA 50: \$?([0-9.]+)',
            'rsi_14': r'RSI 14: ([0-9.]+)',
            'macd_hist': r'hist: ([0-9.-]+)',
            'css_value': r'CSS: ([0-9.-]+)',
            'css_cross_up': r'cross up: (True|true|1|Yes)',
            'css_cross_down': r'cross down: (True|true|1|Yes)',
        }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, prompt, re.IGNORECASE)
            if match:
                val = match.group(1)
                if key in ['css_cross_up', 'css_cross_down']:
                    indicators[key] = val.lower() in ['true', '1', 'yes']
                else:
                    indicators[key] = float(val)
        
        return indicators
    
    def _call_deepseek(self, prompt: str) -> Dict[str, Any]:
        """Call DeepSeek V4 Flash via api.deepseek.com"""
        api_key = self.deepseek_key
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY not set")

        url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 1024,
        }

        start = datetime.utcnow()
        resp = requests.post(url, headers=headers, json=body, timeout=120)
        latency_ms = (datetime.utcnow() - start).total_seconds() * 1000

        if resp.status_code != 200:
            raise ValueError(f"DeepSeek returned {resp.status_code}: {resp.text[:200]}")

        result = resp.json()
        content = result['choices'][0]['message']['content'].strip()

        # Extract JSON from response
        if content.startswith('```'):
            parts = content.split('```')
            for p in parts:
                p = p.strip()
                if p.startswith('{'):
                    content = p
                    break
        if not content.startswith('{'):
            import re
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if m:
                content = m.group()

        decision = json.loads(content.strip())
        decision['latency_ms'] = int(latency_ms)
        decision['tokens'] = result.get('usage', {}).get('total_tokens', 0)
        decision['source'] = 'deepseek'

        return decision
    
    def _validate_sl_tp(self, signal: str, entry_price: float,
                        sl: float, tp: float, atr: float,
                        market_type: str = 'spot') -> tuple:
        """
        Validate and enforce SL/TP rules.
        Returns (validated_sl, validated_tp).
        For market_type='linear' (scalping/futures): SL min 0.3%, TP min 0.6%.
        For market_type='spot' (default): SL min 1.5%, TP min 3%.
        RR >= 1.5 in both. If invalid → returns (None, None) signaling HOLD.
        """
        if signal == 'HOLD':
            return None, None

        if market_type == 'linear':
            min_sl_pct = 0.003   # 0.3% scalping
            min_tp_pct = 0.006   # 0.6% scalping
            max_sl_pct = 0.012   # 1.2% scalping cap
        else:
            min_sl_pct = 0.015   # 1.5% spot
            min_tp_pct = 0.03    # 3% spot
            max_sl_pct = 0.03    # 3% spot cap
        min_rr = 1.5

        if signal == 'BUY':
            if sl is None or tp is None:
                # Auto-calculate if LLM didn't provide
                sl = entry_price * (1 - min_sl_pct)
                tp = entry_price * (1 + min_tp_pct)

            sl_dist_pct = (entry_price - sl) / entry_price
            tp_dist_pct = (tp - entry_price) / entry_price

            # Enforce minimum distances
            if sl_dist_pct < min_sl_pct:
                sl = entry_price * (1 - min_sl_pct)
                sl_dist_pct = min_sl_pct
            if tp_dist_pct < min_tp_pct:
                tp = entry_price * (1 + min_tp_pct)
                tp_dist_pct = min_tp_pct

            # Check RR
            rr = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0
            if rr < min_rr:
                # Try to adjust TP to meet RR
                tp_needed = entry_price * (1 + min_tp_pct)
                tp_dist_needed = min_tp_pct
                sl_dist_pct_fixed = tp_dist_needed / min_rr
                sl_fixed = entry_price * (1 - sl_dist_pct_fixed)

                # If adjusted SL is still reasonable (not too wide)
                if sl_dist_pct_fixed <= max_sl_pct:
                    sl = sl_fixed
                    tp = tp_needed
                else:
                    # Can't meet RR constraints → HOLD
                    return None, None

            return round(sl, 6), round(tp, 6)

        elif signal == 'SELL':
            if sl is None or tp is None:
                sl = entry_price * (1 + min_sl_pct)
                tp = entry_price * (1 - min_tp_pct)

            sl_dist_pct = (sl - entry_price) / entry_price
            tp_dist_pct = (entry_price - tp) / entry_price

            if sl_dist_pct < min_sl_pct:
                sl = entry_price * (1 + min_sl_pct)
                sl_dist_pct = min_sl_pct
            if tp_dist_pct < min_tp_pct:
                tp = entry_price * (1 - min_tp_pct)
                tp_dist_pct = min_tp_pct

            rr = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0
            if rr < min_rr:
                tp_needed = entry_price * (1 - min_tp_pct)
                tp_dist_needed = min_tp_pct
                sl_dist_pct_fixed = tp_dist_needed / min_rr
                sl_fixed = entry_price * (1 + sl_dist_pct_fixed)

                if sl_dist_pct_fixed <= max_sl_pct:
                    sl = sl_fixed
                    tp = tp_needed
                else:
                    return None, None

            return round(sl, 6), round(tp, 6)

        return None, None

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

    def save_decision(self, symbol: str, exchange: str, timeframe: str,
                      indicators: Dict, sentiment: Dict, decision: Dict,
                      rag_context: str = '') -> Optional[int]:
        """Save signal and decision to database, return signal_id (or None if skipped)."""
        # Skip HOLD: log-only, do not pollute DB
        if decision.get('signal') == 'HOLD':
            self.log('info', f"HOLD for {symbol} — not persisted")
            return None

        # Skip duplicate PENDING for same symbol+direction
        with self.db.get_session() as session:
            existing = session.query(Signal).filter(
                Signal.symbol == symbol,
                Signal.status == 'PENDING',
                Signal.signal_type == decision['signal'],
            ).count()
            if existing > 0:
                self.log('info', f"Skipping duplicate PENDING {decision['signal']} for {symbol} ({existing} already pending)")
                return None

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
                model_version=decision.get('source', self.model),
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
                llm_model=decision.get('source', self.model),
                decision_json=decision,
                latency_ms=decision.get('latency_ms', 0),
                total_tokens=decision.get('tokens', 0),
            )
            session.add(decision_log)
            
            return signal_id
    
    def run_once_for_symbol(self, symbol: str, exchange: str = 'binance',
                            timeframe: str = '1h',
                            market_type: str = 'spot') -> Dict[str, Any]:
        """Generate trading decision for a single symbol.

        market_type='linear' enables scalping SL/TP thresholds (0.3%/0.6%).
        """
        
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

        # 8b. Validate and enforce SL/TP rules
        entry_price = indicators.get('price', 0)
        atr = indicators.get('atr_14', 0)
        sl = decision.get('stop_loss')
        tp = decision.get('take_profit')
        sig = decision.get('signal', 'HOLD')

        if sig in ('BUY', 'SELL') and entry_price > 0:
            valid_sl, valid_tp = self._validate_sl_tp(sig, entry_price, sl, tp, atr, market_type=market_type)
            if valid_sl is None and valid_tp is None:
                # Couldn't meet RR constraints → force HOLD
                decision['signal'] = 'HOLD'
                decision['reasoning'] = f"SL/TP constraints not met (RR<1.5) — forced HOLD"
                decision['confidence'] = 0.0
            else:
                decision['stop_loss'] = valid_sl
                decision['take_profit'] = valid_tp

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
        """Generate decisions for all active symbols - scalping mode with 1m Bybit data"""
        self.log('info', "Starting trading decision cycle (scalping mode)...")

        with self.db.get_session() as session:
            from ..core.database import SelectedSymbol
            symbols = session.query(SelectedSymbol).filter_by(is_active=True).all()
            symbol_names = [s.symbol for s in symbols][:10]

        if not symbol_names:
            symbol_names = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']

        # Scalping: use Bybit 1m data for BTC/ETH/SOL
        scalping_symbols = {'BTCUSDT', 'ETHUSDT', 'SOLUSDT'}
        decisions = []
        for sym in symbol_names:
            try:
                # Use Bybit 1m for major scalping pairs, binance 1h for others
                if sym in scalping_symbols:
                    result = self.run_once_for_symbol(sym, exchange='bybit', timeframe='1', market_type='linear')
                else:
                    result = self.run_once_for_symbol(sym, exchange='binance', timeframe='60', market_type='spot')
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
