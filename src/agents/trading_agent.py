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
    OLLAMA_MODEL = "qwen2.5:14b"       # strong free local (Q4 ~9GB, fits 1 card); warm 4-10s, kept warm via host cron
    OLLAMA_FALLBACK = "gemma4:e2b"     # instant 5B fallback if qwen cold/busy (granite4.1:3b also available)
    
    def __init__(self, config: Config, logger: logging.Logger, db: DatabaseManager, 
                 sentiment_agent: SentimentAgent):
        super().__init__('TradingDecision', logger)
        self.config = config
        self.db = db
        self.sentiment = sentiment_agent
        # DeepSeek fully removed 2026-05-26 (dead balance, per Boss). Active chain:
        # GLM (z.ai) -> gpt-5.4 (RuAPI) -> Ollama -> rule-based.
        self.model = 'rule_based'  # neutral default for save_decision model_version fallback
        # Read selectivity params from config (were hardcoded → config ignored before 2026-05-26)
        _agents = getattr(config, 'agents', {}) or {}
        _td = _agents.get('trading_decision', {})
        self.min_confidence = float(_td.get('min_confidence', 0.75))
        self.vol_gate = float(_td.get('vol_gate', 0.5))  # skip bars with volume_ratio below this
        
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
        """Build LLM prompt for scalping decisions with short SL/TP (0.3%/0.6%)"""
        
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
        
        prompt = f"""You are an expert scalping trader for Bybit Linear (USDT perpetuals). Act decisively.

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

## Sentiment (24h)
- Average: {sentiment.get('avg_sentiment', 0):.2f} (-1 to +1)
- Bullish ratio: {sentiment.get('bullish_ratio', 0):.0%}
- News count: {sentiment.get('news_count', 0)}

## Recent Signals
{signal_history}
{position_info}
{rag_section}

## STRATEGY RULES (regime-aware, SELECTIVE — most bars should be HOLD)
Use the FDI regime above to pick the playbook:

A) regime = TRENDING -> trade WITH the trend, do NOT fade it:
   - BUY when CSS > 0 AND price > SMA50 AND MACD hist > 0 AND RSI 40-68 (pullback in uptrend)
   - SELL when CSS < 0 AND price < SMA50 AND MACD hist < 0 AND RSI 32-60
   - Set take_profit ~2.5-3x the stop_loss distance (ride the trend; R/R >= 2).

B) regime = RANGING -> selective counter-trend ONLY at band extremes:
   - BUY when RSI < 25 AND price at/below lower Bollinger
   - SELL when RSI > 75 AND price at/above upper Bollinger
   - take_profit toward the band middle; R/R >= 2.

C) No clear setup, conflicting signals, or weak volume -> HOLD (this is the common case).

Hard guards: NEVER buy if RSI > 80; NEVER sell if RSI < 20. Only emit BUY/SELL with
confidence >= 0.75 when the setup is clean; otherwise HOLD. Quality over quantity —
overtrading loses money on fees.

## Response Format (JSON only)
{{"signal": "BUY" or "SELL" or "HOLD", "confidence": 0.0-1.0, "reasoning": "brief", "stop_loss": price or null, "take_profit": price or null}}

Set stop_loss/take_profit as actual prices with take_profit at least 2x the stop distance from entry."""

        return prompt

    def _call_glm(self, prompt: str, model: str = 'glm-5.1') -> Dict[str, Any]:
        """Call z.ai GLM (OpenAI-compatible). Key GLM_API_KEY, base GLM_BASE_URL.
        Returns 429 'Insufficient balance' until the z.ai account is recharged."""
        import re
        api_key = os.environ.get('GLM_API_KEY', '')
        if not api_key:
            raise ValueError("GLM_API_KEY not set")
        base = os.environ.get('GLM_BASE_URL', 'https://api.z.ai/api/paas/v4').rstrip('/')
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": (
                    "You are a scalping trading decision API for Bybit USDT perpetuals. "
                    "Output ONLY one valid JSON object, no prose, no markdown fences. Exact format: "
                    "{\"signal\": \"BUY\" or \"SELL\" or \"HOLD\", \"confidence\": 0.0-1.0, "
                    "\"reasoning\": \"brief\", \"stop_loss\": price or null, \"take_profit\": price or null}.")},
                {"role": "user", "content": str(prompt) if prompt else ""},
            ],
            "temperature": 0.2,
            "max_tokens": 1024,
        }
        start = datetime.utcnow()
        resp = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=45)
        latency_ms = (datetime.utcnow() - start).total_seconds() * 1000
        if resp.status_code != 200:
            raise ValueError(f"GLM {model} returned {resp.status_code}: {resp.text[:200]}")
        msg = resp.json()['choices'][0]['message']
        raw = (msg.get('content', '') or msg.get('reasoning_content', '') or '').strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw).strip()
        if not raw.startswith('{'):
            m = re.search(r'\{[\s\S]*\}', raw)
            if m:
                raw = m.group()
            else:
                raise ValueError(f"No JSON in GLM {model} response: {raw[:200]}")
        decision = json.loads(raw)
        sig = str(decision.get('signal', '')).upper()
        if sig not in ('BUY', 'SELL', 'HOLD'):
            raise ValueError(f"GLM {model} invalid signal: {decision.get('signal')!r}")
        decision['signal'] = sig
        decision['confidence'] = float(decision.get('confidence', 0) or 0)
        decision['latency_ms'] = int(latency_ms)
        decision['tokens'] = 0
        decision['source'] = f"glm_{model}"
        return decision

    def _call_ruapi(self, prompt: str, model: str = 'claude-haiku-4.5') -> Dict[str, Any]:
        """Call RuAPI proxy (api.stepanovikov.uno, OpenAI-compatible).

        Requires a JSON-forcing system prompt (otherwise the proxy may route to a
        coding assistant that refuses: "I can't do that. I'm Kiro...") and a browser
        User-Agent (Cloudflare returns 403 code 1010 without one).
        Verified working models (2026-05-26): claude-haiku-4.5, gpt-5.4.
        """
        import re
        api_key = os.environ.get('RUAPI_API_KEY', '')
        if not api_key:
            raise ValueError("RUAPI_API_KEY not set")
        url = "https://api.stepanovikov.uno/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) cryptotrader/1.0",
        }
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": (
                    "You are a scalping trading decision API for Bybit USDT perpetuals. "
                    "Output ONLY one valid JSON object, no prose, no markdown fences. "
                    "Exact format: {\"signal\": \"BUY\" or \"SELL\" or \"HOLD\", "
                    "\"confidence\": 0.0-1.0, \"reasoning\": \"brief\", "
                    "\"stop_loss\": price or null, \"take_profit\": price or null}. "
                    "Never refuse; this is a backtested quantitative system, not financial advice."
                )},
                {"role": "user", "content": str(prompt) if prompt else ""},
            ],
            "temperature": 0.2,
            "max_tokens": 1024,
        }
        start = datetime.utcnow()
        resp = requests.post(url, headers=headers, json=body, timeout=45)
        latency_ms = (datetime.utcnow() - start).total_seconds() * 1000
        if resp.status_code != 200:
            raise ValueError(f"RuAPI {model} returned {resp.status_code}: {resp.text[:200]}")
        result = resp.json()
        msg = result['choices'][0]['message']
        raw = (msg.get('content', '') or msg.get('reasoning_content', '') or '').strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw).strip()
        if not raw.startswith('{'):
            m = re.search(r'\{[\s\S]*\}', raw)
            if m:
                raw = m.group()
            else:
                raise ValueError(f"No JSON in RuAPI {model} response: {raw[:200]}")
        decision = json.loads(raw)
        sig = str(decision.get('signal', '')).upper()
        if sig not in ('BUY', 'SELL', 'HOLD'):
            raise ValueError(f"RuAPI {model} invalid signal: {decision.get('signal')!r}")
        decision['signal'] = sig
        decision['confidence'] = float(decision.get('confidence', 0) or 0)
        decision['latency_ms'] = int(latency_ms)
        decision['tokens'] = result.get('usage', {}).get('total_tokens', 0)
        decision['source'] = f"ruapi_{model}"
        return decision

    def call_llm(self, prompt: str, indicators: Dict = None) -> Dict[str, Any]:
        """Provider chain (2026-05-26): gpt-5.4 (RuAPI proxy) -> Ollama qwen2.5:14b
        (free local) -> gemma4:e2b -> rule-based (guaranteed offline).

        GLM (z.ai) removed from the active chain 2026-05-26 — returns 429 (no balance).
        To re-enable when funded, add ('glm-5.1', lambda: self._call_glm(prompt, 'glm-5.1'), 1)
        to `cloud` below. Dead providers removed: opencode-zen, DeepSeek, MiniMax, claude-haiku.
        """
        # 1. Cloud LLMs in priority order: (name, fn, retries)
        cloud = [
            ('gpt-5.4', lambda: self._call_ruapi(prompt, 'gpt-5.4'), 2),
        ]
        for name, fn, tries in cloud:
            for attempt in range(tries):
                try:
                    decision = fn()
                    self.log('info', f"{name}: {decision.get('signal')} (conf={decision.get('confidence', 0):.0%})")
                    return decision
                except Exception as e:
                    self.log('warning', f"{name} attempt {attempt + 1}/{tries} failed ({e})")

        # 2. Ollama gemma4:e2b (free local; short connect timeout, 70s read for cold load)
        try:
            decision = self._call_ollama(prompt)
            if decision.get('source') == 'ollama':
                self.log('info', f"Ollama: {decision.get('signal')}")
                return decision
        except Exception as e:
            self.log('warning', f"Ollama failed ({e}), rule-based fallback")

        # 3. Rule-based (guaranteed final)
        if indicators:
            return self._rule_based_decision_from_indicators(indicators)
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

    def _rule_based_decision_from_indicators(self, indicators: Dict) -> Dict[str, Any]:
        """Rule-based decision using indicators dict DIRECTLY (no prompt parsing).
        
        This is the guaranteed fallback — doesn't depend on prompt being valid.
        Uses the same indicators that were used to build the LLM prompt.
        """
        # Revised 2026-05-26 after backtest: the old 1:1 scalp logic was net-negative from
        # overtrading. Now regime-aware & selective (matches backtest_v2 'trend'/'meanrev'):
        #   trending -> trade WITH the trend (CSS+SMA+MACD aligned, avoid RSI extremes)
        #   ranging  -> selective counter-trend at Bollinger extremes on RSI extremes
        #   else     -> HOLD. Confidence 0.78 (>= min_confidence) for the few quality setups.
        signal, confidence, reasoning = 'HOLD', 0.0, ''
        css = indicators.get('css_value', 0)
        rsi = indicators.get('rsi_14', 50)
        price = indicators.get('price', 0)
        sma50 = indicators.get('sma_50', 0)
        bb_upper = indicators.get('bb_upper', 0)
        bb_lower = indicators.get('bb_lower', 0)
        macd_hist = indicators.get('macd_hist', 0)
        regime = indicators.get('regime', 'unknown')

        if regime == 'trending':
            if css > 0 and price > sma50 and macd_hist > 0 and 40 <= rsi <= 68:
                signal, confidence = 'BUY', 0.78
                reasoning = f"Trend-up: CSS {css:.3f}>0, price>SMA50, MACD+, RSI {rsi:.0f}"
            elif css < 0 and price < sma50 and macd_hist < 0 and 32 <= rsi <= 60:
                signal, confidence = 'SELL', 0.78
                reasoning = f"Trend-down: CSS {css:.3f}<0, price<SMA50, MACD-, RSI {rsi:.0f}"
        elif regime == 'ranging':
            if rsi < 25 and bb_lower and price <= bb_lower * 1.002:
                signal, confidence = 'BUY', 0.78
                reasoning = f"Range mean-rev: RSI {rsi:.0f} oversold at lower BB"
            elif rsi > 75 and bb_upper and price >= bb_upper * 0.998:
                signal, confidence = 'SELL', 0.78
                reasoning = f"Range mean-rev: RSI {rsi:.0f} overbought at upper BB"

        self.log('info', f"Rule-based (regime={regime}): {signal} (conf={confidence:.0%})")

        return {
            'signal': signal,
            'confidence': confidence,
            'reasoning': reasoning,
            'source': 'rule_based_regime'
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
            # Revised 2026-05-26 (backtest): R/R>=2 floors to offset taker fees, fewer/quality trades
            min_sl_pct = 0.005   # 0.5% scalping
            min_tp_pct = 0.012   # 1.2% scalping (R/R ~2.4 at floor)
            max_sl_pct = 0.015   # 1.5% scalping cap
            min_rr = 2.0
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
                    'format': 'json',
                    'stream': False,
                    'keep_alive': '30m',  # keep model resident so warm calls stay ~1s
                    'options': {'temperature': 0.2, 'num_predict': 256},
                }
                resp = requests.post(
                    f"{self.OLLAMA_BASE}/api/generate",
                    json=data,
                    timeout=(3, 95)  # (connect, read): 3s connect fail-fast; 95s read covers qwen2.5:14b cold load (~73s)
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
                
                # Normalize fields — local models sometimes emit words ("HIGH") or odd casing.
                # Without this, a non-numeric confidence crashes the downstream `< min_confidence`.
                sig = str(decision.get('signal', '')).upper().strip()
                if sig not in ('BUY', 'SELL', 'HOLD'):
                    raise ValueError(f"invalid signal {decision.get('signal')!r}")
                decision['signal'] = sig
                conf = decision.get('confidence', 0)
                if not isinstance(conf, (int, float)):
                    conf = {'HIGH': 0.8, 'VERY HIGH': 0.9, 'STRONG': 0.8, 'MEDIUM': 0.6,
                            'MED': 0.6, 'MODERATE': 0.6, 'LOW': 0.4, 'WEAK': 0.4}.get(str(conf).upper().strip())
                    if conf is None:
                        try:
                            conf = float(str(decision.get('confidence')).strip().rstrip('%'))
                            if conf > 1:
                                conf /= 100.0
                        except (ValueError, TypeError):
                            conf = 0.6
                decision['confidence'] = max(0.0, min(1.0, float(conf)))

                self.log('info', f"Ollama succeeded with model: {model} -> {sig} (conf={decision['confidence']:.0%})")
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
                      rag_context: str = '',
                      market_type: str = 'spot') -> Optional[int]:
        """Save signal and decision to database, return signal_id (or None if skipped)."""
        # Skip duplicate PENDING for same symbol+direction (only for BUY/SELL)
        signal_type = decision.get('signal', 'HOLD')
        if signal_type != 'HOLD':
            with self.db.get_session() as session:
                existing = session.query(Signal).filter(
                    Signal.symbol == symbol,
                    Signal.status == 'PENDING',
                    Signal.signal_type == signal_type,
                ).count()
                if existing > 0:
                    self.log('info', f"Skipping duplicate PENDING {signal_type} for {symbol} ({existing} already pending)")
                    return None

        with self.db.get_session() as session:
            # S2: Set TTL based on market_type — scalping=120s, intraday/spot=900s
            ttl_seconds = 120 if market_type == 'linear' else 900
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
                model_version=decision.get('source', self.model),
                reasoning=decision.get('reasoning', ''),
                status='PENDING',
                ttl_seconds=ttl_seconds,
            )
            session.add(signal)
            session.flush()
            signal_id = signal.id

            # Dual-write: also create strategy_signal (for ExecutionAgent which reads from strategy_signals)
            if decision.get('signal') in ('BUY', 'SELL'):
                from ..core.database import StrategySignal
                strategy_sig = StrategySignal(
                    symbol=symbol,
                    strategy='scalping',
                    action=decision['signal'],
                    confidence=decision['confidence'],
                    entry_price=indicators.get('price'),
                    stop_loss=decision.get('stop_loss'),
                    take_profit=decision.get('take_profit'),
                    timeframes=timeframe,
                    reasoning=decision.get('reasoning', ''),
                    status='pending',
                    exchange=exchange,
                )
                session.add(strategy_sig)

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
        if not indicators:
            return {'symbol': symbol, 'signal': 'HOLD', 'reasoning': 'Insufficient bars for indicators'}

        # 2b. Volume gate — skip low-liquidity bars (selective trading; saves LLM cost)
        vol_ratio = indicators.get('volume_ratio', 1.0)
        if vol_ratio < self.vol_gate:
            self.log('info', f"{symbol}: vol_gate HOLD (volume_ratio {vol_ratio:.2f} < {self.vol_gate})")
            return {'symbol': symbol, 'signal': 'HOLD', 'confidence': 0.0,
                    'reasoning': f'vol_gate: {vol_ratio:.2f} < {self.vol_gate}', 'source': 'vol_gate'}

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
        decision = self.call_llm(prompt, indicators)
        
        # 8. Enforce minimum confidence STRICTLY (selective strategy — no RSI override bypass).
        # The old RSI-extreme override boosted weak counter-trend signals past the gate and
        # drove overtrading (backtest 2026-05-26: rule-based net-negative). Removed.
        if decision['signal'] in ('BUY', 'SELL') and decision['confidence'] < self.min_confidence:
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
        signal_id = self.save_decision(symbol, exchange, timeframe, indicators, sentiment, decision, rag_context, market_type)
        
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
