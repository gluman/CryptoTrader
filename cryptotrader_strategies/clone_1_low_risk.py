"""
Clone #1 — Low-risk: 15m, 8 пар, conf=0.5, SL=1%, TP=1%, trailing 0.1%/5min.

Логика ("урвать прибыль, пусть небольшую"):
  - Чаще входим (низкий conf-порог = 0.5)
  - Маленькие SL/TP = 1% (быстрые сделки)
  - Trailing подтягивает SL в безубыток/прибыль по 0.1% каждые 5 минут
  - 8 пар (BTC, ETH, SOL, XRP, DOGE, TON, AVAX, ADA) для диверсификации

По сути: тот же score-engine что Clone0, но с другими параметрами и расширенным списком пар.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from .base_strategy import (
    BaseStrategy,
    StrategyParams,
    compute_atr,
    compute_bollinger,
    compute_css,
    compute_ema,
    compute_macd,
    compute_rsi,
)


class Clone1LowRiskStrategy(BaseStrategy):
    """Клон #1: low-risk scalp на 15m по 8 парам."""

    PARAMS = StrategyParams(
        name="clone1_low_risk",
        timeframe="15m",
        symbols=[
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
            "DOGEUSDT", "TONUSDT", "AVAXUSDT", "ADAUSDT",
        ],
        min_confidence=0.50,         # ↓ с 0.75 (больше входов)
        sl_pct=1.0,                  # фиксированный 1%
        tp_pct=1.0,                  # фиксированный 1%
        trailing_enabled=True,
        trailing_step_pct=0.1,       # шаг 0.1%
        trailing_interval_min=5,     # проверка каждые 5 мин
        max_hold_minutes=240,
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
        atr = compute_atr(highs, lows, closes, 14)
        css = compute_css(closes)
        bb_up, bb_mid, bb_lo = compute_bollinger(closes, 20, 2.0)
        macd_l, macd_s, macd_h = compute_macd(closes)
        vol_sma = pd.Series(vols).rolling(20).mean().values[last]
        vol_ratio = float(vols[last] / vol_sma) if vol_sma > 0 else 1.0

        rsi_val = float(rsi[last]) if not np.isnan(rsi[last]) else 50.0
        css_val = float(css[last]) if not np.isnan(css[last]) else 0.0
        bb_pos = ((price - bb_lo[last]) / (bb_up[last] - bb_lo[last])) if (bb_up[last] - bb_lo[last]) > 1e-10 else 0.5
        macd_h_val = float(macd_h[last]) if not np.isnan(macd_h[last]) else 0.0
        macd_h_prev = float(macd_h[last - 1]) if last >= 1 and not np.isnan(macd_h[last - 1]) else 0.0

        # === Простая scoring-система (как в /tmp/backtest_score_engine.py) ===
        score = 0.0
        reasons = []

        # 1. Price momentum (1h ≈ 4 бара на 15m, 6h ≈ 24 бара)
        ch1h = ((closes[last] - closes[last - 4]) / closes[last - 4] * 100) if last >= 4 else 0.0
        ch6h = ((closes[last] - closes[last - 24]) / closes[last - 24] * 100) if last >= 24 else 0.0
        if ch1h > 0.3:
            score += 0.4
            reasons.append(f"ch1h={ch1h:+.2f}%")
        elif ch1h < -0.3:
            score -= 0.4
            reasons.append(f"ch1h={ch1h:+.2f}%")
        if ch6h > 0.8:
            score += 0.7
            reasons.append(f"ch6h={ch6h:+.2f}%")
        elif ch6h < -0.8:
            score -= 0.7
            reasons.append(f"ch6h={ch6h:+.2f}%")

        # 2. RSI
        if rsi_val < 30:
            score += 0.8
            reasons.append(f"rsi={rsi_val:.0f}<30")
        elif rsi_val < 40:
            score += 0.4
            reasons.append(f"rsi={rsi_val:.0f}<40")
        elif rsi_val > 70:
            score -= 0.8
            reasons.append(f"rsi={rsi_val:.0f}>70")
        elif rsi_val > 60:
            score -= 0.4
            reasons.append(f"rsi={rsi_val:.0f}>60")

        # 3. EMA cross
        cross_now = ema9[last] - ema21[last]
        cross_prev = ema9[last - 1] - ema21[last - 1] if last >= 1 else 0
        if cross_prev < 0 < cross_now:
            score += 0.6
            reasons.append("ema_golden_cross")
        elif cross_prev > 0 > cross_now:
            score -= 0.6
            reasons.append("ema_death_cross")
        elif cross_now > 0:
            score += 0.2
        else:
            score -= 0.2

        # 4. Bollinger position
        if price <= bb_lo[last]:
            score += 0.5
            reasons.append("at_lower_bb")
        elif price >= bb_up[last]:
            score -= 0.5
            reasons.append("at_upper_bb")

        # 5. CSS momentum
        if css_val > 0.5:
            score += 0.4
            reasons.append(f"css={css_val:+.2f}")
        elif css_val < -0.5:
            score -= 0.4
            reasons.append(f"css={css_val:+.2f}")

        # 6. MACD histogram
        if macd_h_val > 0 and macd_h_val > macd_h_prev:
            score += 0.3
            reasons.append("macd_h↑")
        elif macd_h_val < 0 and macd_h_val < macd_h_prev:
            score -= 0.3
            reasons.append("macd_h↓")

        # 7. Volume confirmation (повышает доверие, не меняет знак)
        if vol_ratio > 1.5:
            if score > 0:
                score += 0.2
            elif score < 0:
                score -= 0.2
            reasons.append(f"vol={vol_ratio:.1f}x")

        # === ДЕКОД ===
        signal = "HOLD"
        side = None
        # В clone #1 порог ниже (0.5) → больше сделок
        if score >= 0.8:
            signal = "BUY"
            side = "LONG"
            confidence = min(0.85, 0.55 + abs(score) * 0.1)
        elif score <= -0.8:
            signal = "SELL"
            side = "SHORT"
            confidence = min(0.85, 0.55 + abs(score) * 0.1)
        elif abs(score) >= 0.5:
            # Мягкий сигнал — для клона #1 это ОК (min_conf 0.5)
            if score > 0:
                signal = "BUY"
                side = "LONG"
            else:
                signal = "SELL"
                side = "SHORT"
            confidence = 0.5 + abs(score) * 0.1
        else:
            signal = "HOLD"
            confidence = abs(score) * 0.5

        decision = {
            "signal": signal,
            "confidence": float(confidence),
            "side": side,
            "reasoning": "; ".join(reasons) if reasons else "no_setup",
            "stop_loss_pct": self.params.sl_pct,  # фикс 1%
            "take_profit_pct": self.params.tp_pct,  # фикс 1%
            "score": float(score),
            "regime": "any",
            "details": {
                "rsi": rsi_val, "css": css_val, "bb_pos": bb_pos,
                "vol_ratio": vol_ratio, "ch1h": ch1h, "ch6h": ch6h,
            },
        }
        return decision

    def _hold(self, reason: str, conf: float, details: dict) -> Dict[str, Any]:
        return {
            "signal": "HOLD", "confidence": conf, "side": None,
            "reasoning": reason, "stop_loss_pct": 0.0, "take_profit_pct": 0.0,
            "score": 0.0, "regime": "unknown", "details": details,
        }
