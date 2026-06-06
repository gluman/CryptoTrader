"""
Clone #0 — Текущая прод-стратегия (как в settings.yaml trading_decision).

Конфигурация (settings.yaml, 2026-05-27):
  - TF: 5m
  - Пара: XRPUSDT, DOGEUSDT, TONUSDT
  - min_confidence: 0.75
  - SL: ATR-based, floor 0.30%, k=1.0
  - TP: ATR-based, floor 0.50%, k=1.7
  - Trailing: enabled, activation 0.20%, distance 0.10%
  - Leverage: 1x
  - Position: $5
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from .base_strategy import (
    BaseStrategy,
    StrategyParams,
    compute_adx,
    compute_atr,
    compute_bollinger,
    compute_css,
    compute_ema,
    compute_macd,
    compute_rsi,
)


class Clone0CurrentStrategy(BaseStrategy):
    """Клон #0: копия текущего trading_agent.py в rule-based форме."""

    PARAMS = StrategyParams(
        name="clone0_current",
        timeframe="5m",
        symbols=["XRPUSDT", "DOGEUSDT", "TONUSDT"],
        min_confidence=0.75,
        sl_pct=0.30,   # floor; ATR может расширить
        tp_pct=0.50,   # floor; ATR может расширить
        trailing_enabled=True,
        trailing_step_pct=0.10,         # как в settings
        trailing_interval_min=5,        # пересчёт каждые 5 минут
        max_hold_minutes=240,           # 4ч
        fee_pct=0.1,
    )

    def __init__(self, logger=None):
        super().__init__(self.PARAMS, logger)

    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        if df.empty or len(df) < 60:
            return self._hold("insufficient_data", 0.0, {})

        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        vols = df["volume"].values
        last = len(df) - 1
        price = float(closes[last])

        rsi = compute_rsi(closes, 14)
        ema9 = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        ema50 = compute_ema(closes, 50)
        atr = compute_atr(highs, lows, closes, 14)
        css = compute_css(closes)
        bb_up, bb_mid, bb_lo = compute_bollinger(closes, 20, 2.0)
        macd_l, macd_s, macd_h = compute_macd(closes)
        adx = compute_adx(highs, lows, closes, 14)

        rsi_val = float(rsi[last]) if not np.isnan(rsi[last]) else 50.0
        atr_pct = (float(atr[last]) / price * 100) if price > 0 and not np.isnan(atr[last]) else 0.0
        adx_val = float(adx[last]) if not np.isnan(adx[last]) else 0.0
        css_val = float(css[last]) if not np.isnan(css[last]) else 0.0
        bb_pos = ((price - bb_lo[last]) / (bb_up[last] - bb_lo[last])) if (bb_up[last] - bb_lo[last]) > 1e-10 else 0.5
        macd_h_val = float(macd_h[last]) if not np.isnan(macd_h[last]) else 0.0

        # ATR-based SL/TP (как в execution_agent._adaptive_sl_tp_pct)
        sl_pct = max(self.params.sl_pct, atr_pct * 1.0)
        tp_pct = max(self.params.tp_pct, atr_pct * 1.7)
        # R/R cap (если получается слишком "жирно")
        if tp_pct < sl_pct * 1.5:
            tp_pct = sl_pct * 1.5

        # Regime: trending (ADX>25) vs ranging
        regime = "trending" if adx_val > 25 else "ranging"

        score = 0.0
        bull_signals = 0
        bear_signals = 0
        reasons = []

        if regime == "trending":
            # === TREND-FOLLOWING (как в trading_agent "STRATEGY RULES A") ===
            # LONG: CSS>0 AND price>SMA50 AND MACD hist>0 AND RSI 40-68
            if css_val > 0 and price > ema50[last] and macd_h_val > 0 and 40 <= rsi_val <= 68:
                score = 0.75
                bull_signals = 4
                reasons.append(f"trend_up:css={css_val:.2f}>0,price>EMA50,macd_h>0,rsi={rsi_val:.0f}")
            # SHORT: CSS<0 AND price<SMA50 AND MACD hist<0 AND RSI 32-60
            elif css_val < 0 and price < ema50[last] and macd_h_val < 0 and 32 <= rsi_val <= 60:
                score = 0.75
                bear_signals = 4
                reasons.append(f"trend_down:css={css_val:.2f}<0,price<EMA50,macd_h<0,rsi={rsi_val:.0f}")
            else:
                # Trend-aligned, но не идеальный setup
                if css_val > 0 and price > ema50[last]:
                    score = 0.55
                    bull_signals = 2
                    reasons.append("trend_weak_long")
                elif css_val < 0 and price < ema50[last]:
                    score = -0.55
                    bear_signals = 2
                    reasons.append("trend_weak_short")
        else:
            # === RANGING: contrarian от экстремумов (как "STRATEGY RULES B") ===
            # LONG: RSI<25 AND price<=lower BB
            if rsi_val < 25 and price <= bb_lo[last]:
                score = 0.78
                bull_signals = 3
                reasons.append(f"range_bottom:rsi={rsi_val:.0f}<25,at_lower_bb")
            # SHORT: RSI>75 AND price>=upper BB
            elif rsi_val > 75 and price >= bb_up[last]:
                score = -0.78
                bear_signals = 3
                reasons.append(f"range_top:rsi={rsi_val:.0f}>75,at_upper_bb")
            else:
                # Мягкий mean-reversion сигнал
                if bb_pos < 0.15 and rsi_val < 35:
                    score = 0.45
                    reasons.append(f"range_soft_bottom:bb_pos={bb_pos:.2f}")
                elif bb_pos > 0.85 and rsi_val > 65:
                    score = -0.45
                    reasons.append(f"range_soft_top:bb_pos={bb_pos:.2f}")

        # === ДЕКОДИРОВКА: signal/confidence/side ===
        signal = "HOLD"
        confidence = 0.0
        side = None

        if score >= 0.65:
            signal = "BUY"
            confidence = min(0.95, abs(score))
            side = "LONG"
        elif score <= -0.65:
            signal = "SELL"
            confidence = min(0.95, abs(score))
            side = "SHORT"
        elif abs(score) >= 0.40:
            # Слабый сигнал → HOLD для клона #0 (высокий порог conf=0.75)
            signal = "HOLD"
            confidence = abs(score) * 0.5
        else:
            signal = "HOLD"
            confidence = 0.0

        decision = {
            "signal": signal,
            "confidence": float(confidence),
            "side": side,
            "reasoning": "; ".join(reasons) if reasons else "no_setup",
            "stop_loss_pct": float(sl_pct),
            "take_profit_pct": float(tp_pct),
            "score": float(score),
            "regime": regime,
            "details": {
                "rsi": rsi_val, "atr_pct": atr_pct, "adx": adx_val,
                "css": css_val, "bb_pos": bb_pos, "macd_h": macd_h_val,
            },
        }

        # Clone #0 не имеет contrarian-логики
        return self.is_valid_decision(decision) and decision or self._hold("invalid", 0.0, decision)

    def _hold(self, reason: str, conf: float, details: dict) -> Dict[str, Any]:
        return {
            "signal": "HOLD", "confidence": conf, "side": None,
            "reasoning": reason, "stop_loss_pct": 0.0, "take_profit_pct": 0.0,
            "score": 0.0, "regime": "unknown", "details": details,
        }
