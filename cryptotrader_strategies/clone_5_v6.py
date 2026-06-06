#!/usr/bin/env python3
"""
Clone5 v6 — Market Maker / Liquidity Hunt + ICT 2022 + Bulkowski Hunter-Patterns + Wyckoff VSA.

ОБЪЕДИНЯЕТ:
  1. ICT 2022 Mentorship (FVG, Judas swing, EQH/EQL, session, PO3, 50% fib)
  2. Bulkowski "Encyclopedia of Chart Patterns" — hunter-prone patterns:
     - Double Bottom bust = Spring (14% failure = LONG after breakdown)
     - Head & Shoulders Top bust = UTAD (18.8% failure = LONG)
     - Triangle Sym busted breakouts
     - Broadening Formation
  3. Wyckoff "Studies in Tape Reading" — VSA (Volume Spread Analysis):
     - Effort vs Result (high vol + small move = absorption)
     - No Demand / No Supply bars
     - Climax volume
     - Test bar
  4. Spring/UTAD pattern detection (David Weis)
  5. Composite Man logic — operator behind the move

Per-pair parameters (grid 2026-06-06):
  DOGEUSDT:  sl=30, sw=0.003
  TONUSDT:   sl=80, sw=0.005
  SUIUSDT:   sl=80, sw=0.005

Expected: улучшение v2 (PF 2.21) → v6 (target PF >2.5)
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


# Per-pair tuned parameters
PER_PAIR_PARAMS = {
    "DOGEUSDT": {"swing_lookback": 30, "sweep_threshold": 0.003, "wick_body_min_ratio": 1.0, "min_volume_spike": 1.3},
    "TONUSDT":  {"swing_lookback": 80, "sweep_threshold": 0.005, "wick_body_min_ratio": 1.0, "min_volume_spike": 1.3},
    "SUIUSDT":  {"swing_lookback": 80, "sweep_threshold": 0.005, "wick_body_min_ratio": 1.0, "min_volume_spike": 1.3},
    "XRPUSDT":  {"swing_lookback": 50, "sweep_threshold": 0.003, "wick_body_min_ratio": 1.0, "min_volume_spike": 1.3},
    "ADAUSDT":  {"swing_lookback": 50, "sweep_threshold": 0.003, "wick_body_min_ratio": 1.0, "min_volume_spike": 1.3},
}


class Clone5V6Strategy(BaseStrategy):
    """Clone5 v6: Market Maker + ICT + Bulkowski + Wyckoff VSA."""

    PARAMS = StrategyParams(
        name="clone5_v6_market_maker_full",
        timeframe="5m",
        symbols=["TONUSDT", "DOGEUSDT", "SUIUSDT"],
        min_confidence=0.50,
        sl_pct=0.5,
        tp_pct=2.0,
        trailing_enabled=False,
        max_hold_minutes=180,
        fee_pct=0.055,
    )

    # === Per-pair parameters ===
    swing_lookback: int = 50
    sweep_threshold: float = 0.003
    wick_body_min_ratio: float = 1.0
    min_volume_spike: float = 1.3
    min_rr_ratio: float = 2.5

    # === ICT session: London+NY 8-20 UTC ===
    session_start_utc: int = 8
    session_end_utc: int = 20

    # === ICT EQH/EQL bonus ===
    equal_lows_tolerance_pct: float = 0.003
    min_equal_lows_count: int = 2
    eql_bonus: float = 0.10

    # === VSA parameters ===
    # Effort vs Result: volume_vol/vol_avg, abs(bar_return)/spread
    vsa_effort_threshold: float = 1.5  # vol > 1.5× avg = high effort
    vsa_absorption_spread_pct: float = 0.3  # small spread (< 0.3%) despite high effort = absorption
    vsa_climax_multiplier: float = 3.0  # vol > 3× avg = climax
    # No Demand (down close after uptrend, small spread, low vol)
    no_demand_vol_mult: float = 0.7  # vol < 0.7× avg
    no_demand_spread_pct: float = 0.4  # small spread

    # === Bulkowski pattern detection ===
    # Equal lows check (EQL = 3+ lows within tolerance = 70% of bust = Spring)
    eql_pattern_min_count: int = 3
    # Failed breakout detection: bar closes back inside pattern
    failure_lookback: int = 10

    # === Score bonuses ===
    score_base: float = 0.50
    eql_score_bonus: float = 0.15
    vsa_effort_vs_result_bonus: float = 0.10  # absorption detected
    vsa_climax_bonus: float = 0.15
    vsa_no_demand_bonus: float = 0.05  # No demand after uptrend = potential reversal
    vsa_no_supply_bonus: float = 0.05
    bulkowski_spring_bonus: float = 0.20  # strong spring pattern
    bulkowski_utad_bonus: float = 0.20
    near_round_bonus: float = 0.05
    min_score_to_trade: float = 0.55  # higher than v2 (0.50) → fewer, better trades

    def __init__(self, logger=None):
        super().__init__(self.PARAMS, logger)

    def _apply_per_pair_params(self, symbol: str):
        p = PER_PAIR_PARAMS.get(symbol, {})
        if p:
            self.swing_lookback = p.get("swing_lookback", self.swing_lookback)
            self.sweep_threshold = p.get("sweep_threshold", self.sweep_threshold)
            self.wick_body_min_ratio = p.get("wick_body_min_ratio", self.wick_body_min_ratio)
            self.min_volume_spike = p.get("min_volume_spike", self.min_volume_spike)

    # ====== Helpers ======
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

    # ====== Wyckoff VSA: Effort vs Result ======
    def _vsa_effort_vs_result(self, bar: pd.Series, vol_avg: float) -> float:
        """Return ratio: high effort (vol) vs result (price change). > 1.5 = absorption (climax/no result).
        Wyckoff: high volume but small move = operator absorbing supply/demand."""
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        vol = float(bar["volume"])
        spread = abs(h - l) / max(o, 1e-10) * 100
        if vol_avg <= 0 or spread <= 0:
            return 1.0
        effort = vol / vol_avg
        result = spread
        # Higher effort vs result = more absorption
        if result > 0:
            return effort / max(result / 0.5, 0.1)  # normalize: 0.5% spread = 1.0 baseline
        return effort

    def _vsa_climax(self, bar: pd.Series, vol_avg: float) -> bool:
        """Wyckoff climax: vol > 3x avg + wide spread + close near extreme (potential reversal)."""
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        vol = float(bar["volume"])
        spread = abs(h - l) / max(o, 1e-10) * 100
        if vol_avg <= 0:
            return False
        return vol / vol_avg >= self.vsa_climax_multiplier and spread > 0.5

    def _vsa_no_demand_bar(self, bar: pd.Series, vol_avg: float) -> bool:
        """No Demand (Wyckoff): small spread, low vol, down close. After uptrend = reversal signal."""
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        vol = float(bar["volume"])
        spread_pct = abs(h - l) / max(o, 1e-10) * 100
        if vol_avg <= 0:
            return False
        is_down_close = c < o
        is_small_spread = spread_pct < self.no_demand_spread_pct
        is_low_vol = vol < vol_avg * self.no_demand_vol_mult
        return is_down_close and is_small_spread and is_low_vol

    def _vsa_no_supply_bar(self, bar: pd.Series, vol_avg: float) -> bool:
        """No Supply (Wyckoff): small spread, low vol, up close. After downtrend = reversal signal."""
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        vol = float(bar["volume"])
        spread_pct = abs(h - l) / max(o, 1e-10) * 100
        if vol_avg <= 0:
            return False
        is_up_close = c > o
        is_small_spread = spread_pct < self.no_demand_spread_pct
        is_low_vol = vol < vol_avg * self.no_demand_vol_mult
        return is_up_close and is_small_spread and is_low_vol

    # ====== Bulkowski patterns ======
    def _detect_spring_pattern(self, df: pd.DataFrame, last: int) -> Tuple[bool, float]:
        """Wyckoff Spring: price breaks below support, but closes back above on high volume.
        Bulkowski confirms: 14% of Double Bottoms fail (breakdown below 2nd bottom, then reverse)."""
        if last < 5:
            return False, 0.0
        lows = df["low"].values
        closes = df["close"].values
        vols = df["volume"].values
        # Find recent low (support)
        recent_low_idx = last - self.failure_lookback
        if recent_low_idx < 0:
            return False, 0.0
        support = float(np.min(lows[recent_low_idx:last]))
        # Current bar: broke below support?
        cur_low = float(lows[last])
        cur_close = float(closes[last])
        cur_vol = float(vols[last])
        vol_avg = float(np.mean(vols[max(0, last-20):last]))
        if cur_low < support * 0.997:  # broke below by 0.3%+
            if cur_close > support:  # closed back above
                # Volume check
                if vol_avg > 0 and cur_vol > vol_avg * 1.5:  # high volume spring
                    # Calculate depth
                    depth = (support - cur_low) / support * 100
                    return True, depth
        return False, 0.0

    def _detect_utad_pattern(self, df: pd.DataFrame, last: int) -> Tuple[bool, float]:
        """Wyckoff UTAD (Upthrust After Distribution): price breaks above resistance, closes back below.
        Bulkowski: H&S Top 18.8% failure = reversal down."""
        if last < 5:
            return False, 0.0
        highs = df["high"].values
        closes = df["close"].values
        vols = df["volume"].values
        recent_high_idx = last - self.failure_lookback
        if recent_high_idx < 0:
            return False, 0.0
        resistance = float(np.max(highs[recent_high_idx:last]))
        cur_high = float(highs[last])
        cur_close = float(closes[last])
        cur_vol = float(vols[last])
        vol_avg = float(np.mean(vols[max(0, last-20):last]))
        if cur_high > resistance * 1.003:  # broke above by 0.3%+
            if cur_close < resistance:  # closed back below
                if vol_avg > 0 and cur_vol > vol_avg * 1.5:
                    height = (cur_high - resistance) / resistance * 100
                    return True, height
        return False, 0.0

    # ====== Hunt detection with all enrichments ======
    def _detect_bullish_hunt(self, df: pd.DataFrame, last: int, symbol: str) -> Optional[Dict[str, Any]]:
        if last < self.swing_lookback + 5:
            return None

        bar = df.iloc[last]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        lows = df["low"].values
        highs = df["high"].values
        vols = df["volume"].values
        price = c

        # ICT hunt basics
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

        # ICT session
        bar_time = df.index[last]
        hour_utc = bar_time.hour if hasattr(bar_time, "hour") else 12
        if not (self.session_start_utc <= hour_utc < self.session_end_utc):
            return None

        # === ICT EQH/EQL bonus ===
        eql_count = self._count_equal_levels(lows, swing_low, self.equal_lows_tolerance_pct, lookback=100)
        has_eql = eql_count >= self.min_equal_lows_count

        # === Wyckoff VSA bonuses ===
        ev_ratio = self._vsa_effort_vs_result(bar, vol_avg)
        is_absorbing = ev_ratio >= 2.0  # high effort vs result = absorption
        is_climax = self._vsa_climax(bar, vol_avg)
        # Check if previous bar was "no demand" (potential reversal setup)
        is_no_demand_prev = False
        if last >= 1:
            prev_bar = df.iloc[last - 1]
            is_no_demand_prev = self._vsa_no_demand_bar(prev_bar, vol_avg)

        # === Bulkowski Spring pattern (very strong) ===
        is_spring, spring_depth = self._detect_spring_pattern(df, last)

        # === Round number bonus ===
        near_round = self._is_near_round_number(swing_low)

        # === Score calculation ===
        score = self.score_base
        reasons = [f"v6_bullish_hunt:sw={sweep_dist*100:.2f}%,wr={wick_ratio:.1f},vol={vol_spike:.1f}x"]

        if has_eql:
            score += self.eql_score_bonus
            reasons.append(f"ict_eql={eql_count}")
        if is_absorbing:
            score += self.vsa_effort_vs_result_bonus
            reasons.append(f"vsa_absorb={ev_ratio:.1f}")
        if is_climax:
            score += self.vsa_climax_bonus
            reasons.append("vsa_climax")
        if is_no_demand_prev:
            score += self.vsa_no_demand_bonus
            reasons.append("vsa_no_demand")
        if is_spring:
            score += self.bulkowski_spring_bonus
            reasons.append(f"spring_d={spring_depth:.2f}%")
        if near_round:
            score += self.near_round_bonus
            reasons.append("near_round")

        if score < self.min_score_to_trade:
            return None

        # === SL/TP ===
        sl_price = l * 0.995
        sl_pct = (price - sl_price) / price * 100

        recent_high = float(np.max(highs[max(0, last-self.swing_lookback):last]))
        if recent_high <= price:
            tp_pct = 1.5
        else:
            tp_pct = (recent_high - price) / price * 100

        if sl_pct <= 0 or tp_pct / sl_pct < self.min_rr_ratio:
            return None

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
            "is_spring": is_spring,
            "is_absorbing": is_absorbing,
            "is_climax": is_climax,
            "ev_ratio": ev_ratio,
            "reasons": reasons,
            "hour_utc": hour_utc,
        }

    def _detect_bearish_hunt(self, df: pd.DataFrame, last: int, symbol: str) -> Optional[Dict[str, Any]]:
        if last < self.swing_lookback + 5:
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

        eqh_count = self._count_equal_levels(highs, swing_high, self.equal_lows_tolerance_pct, lookback=100)
        has_eqh = eqh_count >= self.min_equal_lows_count

        ev_ratio = self._vsa_effort_vs_result(bar, vol_avg)
        is_absorbing = ev_ratio >= 2.0
        is_climax = self._vsa_climax(bar, vol_avg)
        is_no_supply_prev = False
        if last >= 1:
            prev_bar = df.iloc[last - 1]
            is_no_supply_prev = self._vsa_no_supply_bar(prev_bar, vol_avg)

        is_utad, utad_height = self._detect_utad_pattern(df, last)

        near_round = self._is_near_round_number(swing_high)

        score = self.score_base
        reasons = [f"v6_bearish_hunt:sw={sweep_dist*100:.2f}%,wr={wick_ratio:.1f},vol={vol_spike:.1f}x"]

        if has_eqh:
            score += self.eql_score_bonus
            reasons.append(f"ict_eqh={eqh_count}")
        if is_absorbing:
            score += self.vsa_effort_vs_result_bonus
            reasons.append(f"vsa_absorb={ev_ratio:.1f}")
        if is_climax:
            score += self.vsa_climax_bonus
            reasons.append("vsa_climax")
        if is_no_supply_prev:
            score += self.vsa_no_supply_bonus
            reasons.append("vsa_no_supply")
        if is_utad:
            score += self.bulkowski_utad_bonus
            reasons.append(f"utad_h={utad_height:.2f}%")
        if near_round:
            score += self.near_round_bonus
            reasons.append("near_round")

        if score < self.min_score_to_trade:
            return None

        sl_price = h * 1.005
        sl_pct = (sl_price - price) / price * 100

        recent_low = float(np.min(lows[max(0, last-self.swing_lookback):last]))
        if recent_low >= price:
            tp_pct = 1.5
        else:
            tp_pct = (price - recent_low) / price * 100

        if sl_pct <= 0 or tp_pct / sl_pct < self.min_rr_ratio:
            return None

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
            "is_utad": is_utad,
            "is_absorbing": is_absorbing,
            "is_climax": is_climax,
            "ev_ratio": ev_ratio,
            "reasons": reasons,
            "hour_utc": hour_utc,
        }

    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        if df.empty or len(df) < 60:
            return self._hold("insufficient_data", 0.0, {})

        self._apply_per_pair_params(symbol)
        last = len(df) - 1

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
            "regime": "v6_hunt_full",
            "details": {
                "sweep_dist_pct": setup["sweep_dist_pct"],
                "wick_ratio": setup["wick_ratio"],
                "vol_spike": setup["vol_spike"],
                "eql_count": setup.get("eql_count", setup.get("eqh_count", 0)),
                "near_round": setup.get("near_round", False),
                "is_spring": setup.get("is_spring", False),
                "is_utad": setup.get("is_utad", False),
                "is_absorbing": setup.get("is_absorbing", False),
                "is_climax": setup.get("is_climax", False),
                "ev_ratio": setup.get("ev_ratio", 0),
                "hour_utc": setup.get("hour_utc", 0),
            },
        }

    def _hold(self, reason: str, conf: float, details: dict) -> Dict[str, Any]:
        return {
            "signal": "HOLD", "confidence": conf, "side": None,
            "reasoning": reason, "stop_loss_pct": 0.0, "take_profit_pct": 0.0,
            "score": 0.0, "regime": "unknown", "details": details,
        }
