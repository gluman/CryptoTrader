"""
Clone #4 — Bollinger Squeeze Breakout PRO (для 1h BTC/ETH).

Только ликвидные пары, 1h TF, супер-строгие фильтры, R:R=3+.
Работает в trending, не в ranging. Сделки редкие, но качественные.
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
    """Клон #4: BB Squeeze PRO (1h, ликвидные пары, R:R=3+)."""

    PARAMS = StrategyParams(
        name="clone4_bb_squeeze",
        timeframe="1h",
        symbols=["BTCUSDT", "ETHUSDT"],   # только ликвидные
        min_confidence=0.70,
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

        bb_width = float(bb_up[last] - bb_lo[last])
        bb_width_avg = float(np.mean([bb_up[i] - bb_lo[i] for i in range(max(0, last-19), last+1)]))
        bb_squeeze_ratio = bb_width / bb_width_avg if bb_width_avg > 0 else 1.0

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

        # === 1. BB Squeeze: bands сузились (< 70% от средней ширины) ===
        squeeze_active = bb_squeeze_ratio < 0.70

        # === 2. Расширение bands: текущая ширина > средней (breakout в действии) ===
        expanding = bb_squeeze_ratio > 1.0

        # === 3. Volume spike: подтверждение пробоя ===
        volume_spike = vol_ratio > 1.5

        # === 4. Направление через EMA + MACD ===
        ema_bull = ema9[last] > ema21[last] and price > ema50[last]
        ema_bear = ema9[last] < ema21[last] and price < ema50[last]

        # === ENTRY: squeeze + expanding + volume + direction ===
        if squeeze_active and expanding and volume_spike:
            if ema_bull and macd_h_val > 0 and macd_rising:
                score = 0.90
                reasons.append(f"bb_squeeze_breakout_long:sq={bb_squeeze_ratio:.2f},vol={vol_ratio:.1f}x,ema_bull,macd↑")
            elif ema_bear and macd_h_val < 0 and not macd_rising:
                score = -0.90
                reasons.append(f"bb_squeeze_breakout_short:sq={bb_squeeze_ratio:.2f},vol={vol_ratio:.1f}x,ema_bear,macd↓")

        # === FALLBACK: trending (ADX>20) + EMA cross + volume ===
        if score == 0.0 and adx_val > 20 and volume_spike:
            if ema_bull and macd_h_val > 0:
                score = 0.70
                reasons.append(f"trending_long:adx={adx_val:.1f},vol={vol_ratio:.1f}x,ema_bull")
            elif ema_bear and macd_h_val < 0:
                score = -0.70
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
            "regime": "squeeze_breakout" if squeeze_active and expanding else ("trending" if adx_val > 20 else "ranging"),
            "details": {
                "rsi": rsi_val, "adx": adx_val, "bb_squeeze_ratio": bb_squeeze_ratio,
                "atr_pct": atr_pct, "vol_ratio": vol_ratio, "macd_h": macd_h_val,
                "squeeze_active": squeeze_active, "expanding": expanding, "volume_spike": volume_spike,
            },
        }
        return decision

    def _hold(self, reason: str, conf: float, details: dict) -> Dict[str, Any]:
        return {
            "signal": "HOLD", "confidence": conf, "side": None,
            "reasoning": reason, "stop_loss_pct": 0.0, "take_profit_pct": 0.0,
            "score": 0.0, "regime": "unknown", "details": details,
        }
