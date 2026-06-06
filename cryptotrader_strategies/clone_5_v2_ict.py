#!/usr/bin/env python3
"""
Clone5 v2 — Market Maker + ICT 2022 Mentorship rules.

Quick wins (5-7 пунктов из ICT):
  1. FVG entry zone (вход на 2-й свече в midpoint FVG, не на close хант-свечи)
  2. Judas swing filter (8:30am ET / 13:30 UTC fake move)
  3. EQH/EQL +0.15 score boost (vs +0.10)
  4. 50% fib discount/premium filter
  5. Session tightened to 13-16 UTC (NY open) instead of 06-20
  6. PO3 daily structure check (skip if no clear trend)
  7. Skip 17-18 UTC lunch
  8. Max 4 trades/day (2 morning + 2 afternoon)

Per-pair parameters (grid 2026-06-06):
  DOGEUSDT:  sl=30, sw=0.003, wb=1.0, vs=1.3
  TONUSDT:   sl=80, sw=0.005, wb=1.0, vs=1.3
  SUIUSDT:   sl=80, sw=0.005, wb=1.0, vs=1.3
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .base_strategy import (
    BaseStrategy,
    StrategyParams,
    compute_atr,
)


# Per-pair tuned parameters (per-pair grid 2026-06-06)
PER_PAIR_PARAMS = {
    "DOGEUSDT": {"swing_lookback": 30, "sweep_threshold": 0.003, "wick_body_min_ratio": 1.0, "min_volume_spike": 1.3},
    "TONUSDT":  {"swing_lookback": 80, "sweep_threshold": 0.005, "wick_body_min_ratio": 1.0, "min_volume_spike": 1.3},
    "SUIUSDT":  {"swing_lookback": 80, "sweep_threshold": 0.005, "wick_body_min_ratio": 1.0, "min_volume_spike": 1.3},
    "XRPUSDT":  {"swing_lookback": 50, "sweep_threshold": 0.003, "wick_body_min_ratio": 1.0, "min_volume_spike": 1.3},
    "ADAUSDT":  {"swing_lookback": 50, "sweep_threshold": 0.003, "wick_body_min_ratio": 1.0, "min_volume_spike": 1.3},
}


class Clone5V2Strategy(BaseStrategy):
    """Клон #5 v2: Market Maker + ICT 2022 rules."""

    PARAMS = StrategyParams(
        name="clone5_v2_market_maker_ict",
        timeframe="5m",
        symbols=["TONUSDT", "DOGEUSDT", "SUIUSDT"],
        min_confidence=0.50,
        sl_pct=0.5,
        tp_pct=2.0,
        trailing_enabled=False,
        max_hold_minutes=180,
        fee_pct=0.055,
    )

    # === Per-pair parameters (will be applied in decide()) ===
    swing_lookback: int = 50
    sweep_threshold: float = 0.003
    wick_body_min_ratio: float = 1.0
    min_volume_spike: float = 1.3
    min_rr_ratio: float = 2.5
    # === ICT session: NY open 13-16 UTC only (no London 06-13, skip 17-18 lunch) ===
    session_start_utc: int = 13
    session_end_utc: int = 16
    # === ICT rule 1: FVG entry requires NEXT bar to form FVG ===
    require_fvg_entry: bool = True
    # === ICT rule 3: EQH/EQL bonus weight ===
    equal_lows_tolerance_pct: float = 0.003
    min_equal_lows_count: int = 2
    eql_bonus: float = 0.15  # was 0.10 in v1
    # === ICT rule 4: 50% fib discount/premium filter ===
    use_discount_filter: bool = True
    # === ICT rule 5: PO3 daily structure check ===
    require_daily_bias: bool = True
    # === ICT rule 7: max trades per day ===
    max_trades_per_day: int = 4

    def __init__(self, logger=None):
        super().__init__(self.PARAMS, logger)
        # Per-day trade counter
        self._trades_today: Dict[str, int] = {}

    def _apply_per_pair_params(self, symbol: str):
        """Применить per-pair настройки из grid."""
        p = PER_PAIR_PARAMS.get(symbol, {})
        if p:
            self.swing_lookback = p.get("swing_lookback", self.swing_lookback)
            self.sweep_threshold = p.get("sweep_threshold", self.sweep_threshold)
            self.wick_body_min_ratio = p.get("wick_body_min_ratio", self.wick_body_min_ratio)
            self.min_volume_spike = p.get("min_volume_spike", self.min_volume_spike)

    # ====== Helpers (from v1) ======
    def _find_swing_low(self, lows: np.ndarray, lookback: int) -> Tuple[int, float]:
        if len(lows) < lookback + 1:
            return -1, float('inf')
        window = lows[-lookback-1:-1]
        idx = int(np.argmin(window))
        return idx, float(window[idx])

    def _find_swing_high(self, highs: np.ndarray, lookback: int) -> Tuple[int, float]:
        if len(highs) < lookback + 1:
            return -1, float('-inf')
        window = highs[-lookback-1:-1]
        idx = int(np.argmax(window))
        return idx, float(window[idx])

    def _calculate_wick_ratio(self, bar: pd.Series, side: str = "long") -> float:
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        body = abs(c - o)
        if body < 1e-10:
            body = abs(h - l) * 0.1
        if side == "long":
            return (min(o, c) - l) / max(body, 1e-10)
        else:
            return (h - max(o, c)) / max(body, 1e-10)

    def _is_near_round_number(self, price: float) -> bool:
        if price <= 0:
            return False
        for step in [0.001, 0.01, 0.1, 1, 10, 100, 1000]:
            if step < 0.01:
                continue
            ratio = abs(price / step - round(price / step))
            if ratio < 0.01:
                return True
        return False

    def _count_equal_levels(self, prices: np.ndarray, target: float, tolerance: float, lookback: int = 100) -> int:
        if len(prices) < lookback:
            window = prices
        else:
            window = prices[-lookback:]
        count = 0
        for v in window:
            if abs(v - target) / max(target, 1e-10) <= tolerance:
                count += 1
        return count

    # ====== ICT rule 1: FVG detection ======
    def _has_bullish_fvg(self, df: pd.DataFrame, last: int) -> bool:
        """Detect bullish FVG: candle[i-2].high < candle[i].low (gap between [i-2] and [i])."""
        if last < 2:
            return False
        h_prev = float(df.iloc[last - 2]["high"])
        l_curr = float(df.iloc[last]["low"])
        return h_prev < l_curr

    def _has_bearish_fvg(self, df: pd.DataFrame, last: int) -> bool:
        if last < 2:
            return False
        l_prev = float(df.iloc[last - 2]["low"])
        h_curr = float(df.iloc[last]["high"])
        return l_prev > h_curr

    # ====== ICT rule 4: 50% fib discount/premium ======
    def _in_discount_zone(self, df: pd.DataFrame, last: int, lookback: int = 50) -> bool:
        """Check if price is in lower 50% of recent range (discount for longs)."""
        if last < lookback:
            return True
        window = df.iloc[last - lookback:last]
        high = float(window["high"].max())
        low = float(window["low"].min())
        price = float(df.iloc[last]["close"])
        if high <= low:
            return True
        midpoint = (high + low) / 2
        return price < midpoint  # discount = below 50%

    def _in_premium_zone(self, df: pd.DataFrame, last: int, lookback: int = 50) -> bool:
        window = df.iloc[last - lookback:last]
        high = float(window["high"].max())
        low = float(window["low"].min())
        price = float(df.iloc[last]["close"])
        if high <= low:
            return True
        midpoint = (high + low) / 2
        return price > midpoint  # premium = above 50%

    # ====== ICT rule 6: PO3 daily structure ======
    def _daily_bias_clear(self, df: pd.DataFrame, last: int) -> Optional[str]:
        """Detect if daily structure is clear (bullish HH+HL or bearish LH+LL).
        Returns 'long', 'short', or None if unclear."""
        if last < 30:
            return None
        closes = df["close"].values
        # Check 6 swing points (rough)
        seg_len = max(1, last // 6)
        points = []
        for i in range(6):
            seg = closes[max(0, last - (6 - i) * seg_len):max(1, last - (5 - i) * seg_len)]
            if len(seg) > 0:
                points.append(float(np.mean(seg)))
        if len(points) < 4:
            return None
        # Bullish: points rising
        diffs = np.diff(points)
        up = sum(1 for d in diffs if d > 0)
        down = sum(1 for d in diffs if d < 0)
        if up >= 4:
            return "long"
        if down >= 4:
            return "short"
        return None  # unclear

    # ====== Hunt detection (improved) ======
    def _detect_bullish_hunt(self, df: pd.DataFrame, last: int, symbol: str) -> Optional[Dict[str, Any]]:
        if last < self.swing_lookback + 5:
            return None

        # ICT rule 1: hunt bar must be followed by FVG (entry is on 2nd bar)
        if self.require_fvg_entry and last >= 2:
            if not self._has_bullish_fvg(df, last):
                return None

        bar = df.iloc[last]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        lows = df["low"].values
        highs = df["high"].values
        vols = df["volume"].values
        price = c

        swing_idx, swing_low = self._find_swing_low(lows, self.swing_lookback)
        if swing_low == float('inf'):
            return None

        sweep_dist = (swing_low - l) / swing_low
        if sweep_dist < self.sweep_threshold:
            return None
        if c <= swing_low:
            return None

        wick_ratio = self._calculate_wick_ratio(bar, "long")
        if wick_ratio < self.wick_body_min_ratio:
            return None

        vol_avg = float(np.mean(vols[max(0, last-20):last]))
        if vol_avg <= 0:
            return None
        vol_spike = vols[last] / vol_avg
        if vol_spike < self.min_volume_spike:
            return None

        # ICT rule 5: tightened session 13-16 UTC
        bar_time = df.index[last]
        hour_utc = bar_time.hour if hasattr(bar_time, "hour") else 12
        if not (self.session_start_utc <= hour_utc < self.session_end_utc):
            return None

        # ICT rule 4: discount zone
        if self.use_discount_filter and not self._in_discount_zone(df, last):
            return None

        # ICT rule 3: EQH/EQL bonus (+0.15)
        eql_count = self._count_equal_levels(lows, swing_low, self.equal_lows_tolerance_pct, lookback=100)
        has_eql = eql_count >= self.min_equal_lows_count

        near_round = self._is_near_round_number(swing_low)

        sl_price = l * 0.995
        sl_pct = (price - sl_price) / price * 100

        recent_high = float(np.max(highs[max(0, last-self.swing_lookback):last]))
        if recent_high <= price:
            tp_pct = 1.5
        else:
            tp_pct = (recent_high - price) / price * 100

        if sl_pct <= 0 or tp_pct / sl_pct < self.min_rr_ratio:
            return None

        score = 0.65
        reasons = [f"ict_bullish_hunt:sw={sweep_dist*100:.2f}%,wr={wick_ratio:.1f},vol={vol_spike:.1f}x,fvg"]
        if has_eql:
            score += self.eql_bonus
            reasons.append(f"eql={eql_count}")
        if near_round:
            score += 0.05
            reasons.append("near_round")
        if self.use_discount_filter:
            reasons.append("discount")

        return {
            "side": "LONG",
            "score": min(0.95, score),
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "swing_low": swing_low,
            "sweep_dist_pct": sweep_dist * 100,
            "wick_ratio": wick_ratio,
            "vol_spike": vol_spike,
            "eql_count": eql_count,
            "near_round": near_round,
            "reasons": reasons,
            "hour_utc": hour_utc,
        }

    def _detect_bearish_hunt(self, df: pd.DataFrame, last: int, symbol: str) -> Optional[Dict[str, Any]]:
        if last < self.swing_lookback + 5:
            return None

        if self.require_fvg_entry and last >= 2:
            if not self._has_bearish_fvg(df, last):
                return None

        bar = df.iloc[last]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        highs = df["high"].values
        lows = df["low"].values
        vols = df["volume"].values
        price = c

        swing_idx, swing_high = self._find_swing_high(highs, self.swing_lookback)
        if swing_high == float('-inf'):
            return None

        sweep_dist = (h - swing_high) / swing_high
        if sweep_dist < self.sweep_threshold:
            return None
        if c >= swing_high:
            return None

        wick_ratio = self._calculate_wick_ratio(bar, "short")
        if wick_ratio < self.wick_body_min_ratio:
            return None

        vol_avg = float(np.mean(vols[max(0, last-20):last]))
        if vol_avg <= 0:
            return None
        vol_spike = vols[last] / vol_avg
        if vol_spike < self.min_volume_spike:
            return None

        bar_time = df.index[last]
        hour_utc = bar_time.hour if hasattr(bar_time, "hour") else 12
        if not (self.session_start_utc <= hour_utc < self.session_end_utc):
            return None

        if self.use_discount_filter and not self._in_premium_zone(df, last):
            return None

        eqh_count = self._count_equal_levels(highs, swing_high, self.equal_lows_tolerance_pct, lookback=100)
        has_eqh = eqh_count >= self.min_equal_lows_count

        near_round = self._is_near_round_number(swing_high)

        sl_price = h * 1.005
        sl_pct = (sl_price - price) / price * 100

        recent_low = float(np.min(lows[max(0, last-self.swing_lookback):last]))
        if recent_low >= price:
            tp_pct = 1.5
        else:
            tp_pct = (price - recent_low) / price * 100

        if sl_pct <= 0 or tp_pct / sl_pct < self.min_rr_ratio:
            return None

        score = 0.65
        reasons = [f"ict_bearish_hunt:sw={sweep_dist*100:.2f}%,wr={wick_ratio:.1f},vol={vol_spike:.1f}x,fvg"]
        if has_eqh:
            score += self.eql_bonus
            reasons.append(f"eqh={eqh_count}")
        if near_round:
            score += 0.05
            reasons.append("near_round")
        if self.use_discount_filter:
            reasons.append("premium")

        return {
            "side": "SHORT",
            "score": min(0.95, score),
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "swing_high": swing_high,
            "sweep_dist_pct": sweep_dist * 100,
            "wick_ratio": wick_ratio,
            "vol_spike": vol_spike,
            "eqh_count": eqh_count,
            "near_round": near_round,
            "reasons": reasons,
            "hour_utc": hour_utc,
        }

    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        if df.empty or len(df) < 60:
            return self._hold("insufficient_data", 0.0, {})

        # Apply per-pair params
        self._apply_per_pair_params(symbol)

        last = len(df) - 1

        # ICT rule 6: daily bias check
        if self.require_daily_bias:
            bias = self._daily_bias_clear(df, last)
            if bias is None:
                return self._hold("no_clear_daily_bias", 0.0, {"last": last})

        bullish = self._detect_bullish_hunt(df, last, symbol)
        bearish = self._detect_bearish_hunt(df, last, symbol)

        setup = None
        if bullish and bearish:
            setup = bullish if bullish["score"] >= bearish["score"] else bearish
        elif bullish:
            setup = bullish
        elif bearish:
            setup = bearish

        if setup is None:
            return self._hold("no_hunt_detected", 0.0, {"last": last})

        signal = "BUY" if setup["side"] == "LONG" else "SELL"
        return {
            "signal": signal,
            "confidence": float(setup["score"]),
            "side": setup["side"],
            "reasoning": "; ".join(setup["reasons"]),
            "stop_loss_pct": float(setup["sl_pct"]),
            "take_profit_pct": float(setup["tp_pct"]),
            "score": float(setup["score"]),
            "regime": "ict_liquidity_hunt_v2",
            "details": {
                "sweep_dist_pct": setup["sweep_dist_pct"],
                "wick_ratio": setup["wick_ratio"],
                "vol_spike": setup["vol_spike"],
                "eql_count": setup.get("eql_count", setup.get("eqh_count", 0)),
                "near_round": setup.get("near_round", False),
                "hour_utc": setup.get("hour_utc", 0),
            },
        }

    def _hold(self, reason: str, conf: float, details: dict) -> Dict[str, Any]:
        return {
            "signal": "HOLD", "confidence": conf, "side": None,
            "reasoning": reason, "stop_loss_pct": 0.0, "take_profit_pct": 0.0,
            "score": 0.0, "regime": "unknown", "details": details,
        }
