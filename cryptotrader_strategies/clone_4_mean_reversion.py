"""
Clone #4 — Bollinger Squeeze Breakout (v2: реалистичный фильтр).

Логика:
  - Сжатие BB (squeeze) было N баров назад → сейчас bands расширяются = breakout
  - Направление: EMA cross + MACD + ADX
  - Volume spike подтверждает пробой
  - R:R = 3+ (TP=3%, SL=1%) чтобы быть прибыльным при WR 40-50%
  - Trailing ОТКЛЮЧЁН (он портил R:R в тестах)
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
    compute_ema,
    compute_macd,
    compute_rsi,
)


class Clone4MeanReversionStrategy(BaseStrategy):
    """Клон #4: BB Squeeze Breakout v2 (реалистичный squeeze-then-expand)."""

    PARAMS = StrategyParams(
        name="clone4_bb_squeeze",
        timeframe="15m",
        symbols=[
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
            "DOGEUSDT", "TONUSDT", "AVAXUSDT", "ADAUSDT",
        ],
        min_confidence=0.55,
        sl_pct=1.0,
        tp_pct=3.0,                 # R:R = 3
        trailing_enabled=False,
        max_hold_minutes=240,
        fee_pct=0.055,
    )

    def __init__(self, params: Optional[StrategyParams] = None, logger=None):
        if params is None:
            params = self.PARAMS
        super().__init__(params, logger)

    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        if df.empty or len(df) < 50:
            return self._hold("insufficient_data", 0.0, {})

        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        vols = df["volume"].values
        last = len(df) - 1
        price = float(closes[last])

        bb_up, bb_mid, bb_lo = compute_bollinger(closes, 20, 2.0)
        ema9 = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        ema50 = compute_ema(closes, 50)
        atr = compute_atr(highs, lows, closes, 14)
        rsi = compute_rsi(closes, 14)
        adx = compute_adx(highs, lows, closes, 14)
        macd_l, macd_s, macd_h = compute_macd(closes)

        # === Реалистичный squeeze-then-expand: проверяем squeeze 5-15 баров назад + expanding сейчас ===
        bb_width_now = float(bb_up[last] - bb_lo[last])
        bb_width_avg_now = float(np.mean([bb_up[i] - bb_lo[i] for i in range(max(0, last-19), last+1)]))
        expand_ratio = bb_width_now / bb_width_avg_now if bb_width_avg_now > 0 else 1.0
        expanding_now = expand_ratio > 1.15  # bands расширяются на 15%+

        # Был ли squeeze 5-15 баров назад?
        was_squeezed = False
        for j in range(max(0, last - 15), max(0, last - 4)):
            bb_w_j = float(bb_up[j] - bb_lo[j])
            bb_w_avg_j = float(np.mean([bb_up[k] - bb_lo[k] for k in range(max(0, j-19), j+1)]))
            if bb_w_avg_j > 0 and (bb_w_j / bb_w_avg_j) < 0.70:
                was_squeezed = True
                break

        rsi_val = float(rsi[last]) if not np.isnan(rsi[last]) else 50.0
        adx_val = float(adx[last]) if not np.isnan(adx[last]) else 0.0
        atr_pct = (float(atr[last]) / price * 100) if price > 0 and not np.isnan(atr[last]) else 0.0
        vol_sma = float(np.mean(vols[max(0, last-20):last+1]))
        vol_ratio = float(vols[last] / vol_sma) if vol_sma > 0 else 1.0
        macd_h_val = float(macd_h[last]) if not np.isnan(macd_h[last]) else 0.0
        macd_h_prev = float(macd_h[last-1]) if last >= 1 and not np.isnan(macd_h[last-1]) else 0.0
        macd_rising = macd_h_val > macd_h_prev

        score = 0.0
        reasons = []

        # === ENTRY: was_squeezed + expanding + volume + direction ===
        if was_squeezed and expanding_now and vol_ratio > 1.0:
            ema_bull = ema9[last] > ema21[last] and price > ema50[last]
            ema_bear = ema9[last] < ema21[last] and price < ema50[last]

            # LONG breakout
            if ema_bull and macd_h_val > 0:
                score = 0.85
                reasons.append(f"squeeze_breakout_long:sq_exp={expand_ratio:.2f},vol={vol_ratio:.1f}x,ema_bull,macd+")
            # SHORT breakout
            elif ema_bear and macd_h_val < 0:
                score = -0.85
                reasons.append(f"squeeze_breakout_short:sq_exp={expand_ratio:.2f},vol={vol_ratio:.1f}x,ema_bear,macd-")

        # === FALLBACK: trending (ADX>20) + volume + EMA cross ===
        if score == 0.0 and adx_val > 20 and vol_ratio > 1.0:
            ema_bull = ema9[last] > ema21[last] and price > ema50[last]
            ema_bear = ema9[last] < ema21[last] and price < ema50[last]
            if ema_bull and macd_h_val > 0:
                score = 0.65
                reasons.append(f"trending_long:adx={adx_val:.1f},vol={vol_ratio:.1f}x,ema_bull")
            elif ema_bear and macd_h_val < 0:
                score = -0.65
                reasons.append(f"trending_short:adx={adx_val:.1f},vol={vol_ratio:.1f}x,ema_bear")

        # === SL/TP ===
        sl_pct = max(self.params.sl_pct, atr_pct * 1.2)
        tp_pct = max(self.params.tp_pct, atr_pct * 2.5, sl_pct * 3.0)  # R:R >= 3

        signal = "HOLD"
        side = None
        confidence = 0.0
        if score >= 0.70:
            signal = "BUY"
            side = "LONG"
            confidence = min(0.95, abs(score))
        elif score <= -0.70:
            signal = "SELL"
            side = "SHORT"
            confidence = min(0.95, abs(score))

        decision = {
            "signal": signal,
            "confidence": float(confidence),
            "side": side,
            "reasoning": "; ".join(reasons) if reasons else "no_setup",
            "stop_loss_pct": float(sl_pct),
            "take_profit_pct": float(tp_pct),
            "score": float(score),
            "regime": "squeeze_breakout" if was_squeezed and expanding_now else ("trending" if adx_val > 20 else "ranging"),
            "details": {
                "rsi": rsi_val, "adx": adx_val, "expand_ratio": expand_ratio,
                "atr_pct": atr_pct, "vol_ratio": vol_ratio, "macd_h": macd_h_val,
                "was_squeezed": was_squeezed, "expanding_now": expanding_now,
            },
        }
        return decision

    def _hold(self, reason: str, conf: float, details: dict) -> Dict[str, Any]:
        return {
            "signal": "HOLD", "confidence": conf, "side": None,
            "reasoning": reason, "stop_loss_pct": 0.0, "take_profit_pct": 0.0,
            "score": 0.0, "regime": "unknown", "details": details,
        }
