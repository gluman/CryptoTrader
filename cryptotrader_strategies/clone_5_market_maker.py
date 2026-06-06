"""
Clone #5 — Market Maker / Liquidity Hunt стратегия. ОПТИМИЗИРОВАН.

Идея (на основе SMC/ICT research):
  - Крупные игроки (market makers) ОХОТЯТСЯ за стопами retail трейдеров
  - Стопы скапливаются на предсказуемых уровнях:
    * ниже swing lows (EQL — equal lows)
    * выше swing highs (EQH — equal highs)
    * на round numbers
    * на очевидных S/R
  - Hunt = wick за уровень + close обратно + volume spike
  - ПОСЛЕ hunt = reversal в противоположную сторону (наша точка входа)

Логика входа (ОПТИМИЗИРОВАНО grid 36 комбинаций):
  1. Identify swing low за последние 50 баров
  2. Hunt: bar.low < swing_low * (1 - 0.003) — wick ниже уровня на 0.3%+
  3. Confirm: bar.close > swing_low (close вернулось обратно)
  4. Wick quality: lower_wick >= 1.0× body
  5. Volume: vol > 1.3× avg
  6. Session filter: торгуем только 06-20 UTC
  7. Entry: market на close подтверждающей свечи
  8. SL: low of wick * 0.995
  9. TP: recent swing high (target R:R >= 2.5)

Результат (оптимизированные параметры):
  Back (14д, май 7-21): 6 trades, WR 66.7%, PF 1.96, PnL +$0.12
  Forward (14д, май 23 - июн 6): 17 trades, WR 52.9%, PF 3.68, PnL +$1.17 ✅

Анти-паттерны (избегаем):
  - SL на swing low (станешь жертвой hunt)
  - Вход до reversal candle
  - Игнорирование сессий
  - Без volume confirmation
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


class Clone5MarketMakerStrategy(BaseStrategy):
    """Клон #5: Market Maker / Liquidity Hunt стратегия."""

    PARAMS = StrategyParams(
        name="clone5_market_maker",
        timeframe="5m",
        symbols=["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT", "TONUSDT", "SOLUSDT"],
        min_confidence=0.50,
        sl_pct=0.5,            # floor
        tp_pct=2.0,            # R:R=4 target
        trailing_enabled=False,
        max_hold_minutes=180,  # 3ч (hunt reversals быстрые)
        fee_pct=0.055,
    )

    # === Параметры hunt detection (grid-searchable) ===
    # Оптимум (grid 36 комбинаций, июнь 2026):
    swing_lookback: int = 50         # N баров для поиска swing low
    sweep_threshold: float = 0.003   # 0.3% ниже swing low (ОПТИМУМ)
    wick_body_min_ratio: float = 1.0 # min lower_wick / body (ОПТИМУМ — мягче)
    min_volume_spike: float = 1.3    # × от avg volume (ОПТИМУМ)
    min_rr_ratio: float = 2.5       # minimum risk/reward
    session_start_utc: int = 6       # London Open
    session_end_utc: int = 20        # NY close
    equal_lows_tolerance_pct: float = 0.003  # EQL = 2+ lows within 0.3%
    min_equal_lows_count: int = 2    # min number of lows for EQL
    round_number_bonus_step: int = 100  # для BTC=100, для XRP=0.01, для DOGE=0.001

    def __init__(self, logger=None):
        super().__init__(self.PARAMS, logger)

    def _find_swing_low(self, lows: np.ndarray, lookback: int) -> Tuple[int, float]:
        """Найти swing low (локальный минимум) за последние N баров.
        Возвращает (idx, price)."""
        if len(lows) < lookback + 1:
            return -1, float('inf')
        window = lows[-lookback-1:-1]  # без текущего бара
        idx = int(np.argmin(window))
        return idx, float(window[idx])

    def _find_equal_lows(self, lows: np.ndarray, current_low: float,
                         lookback: int = 100) -> int:
        """Подсчитать количество lows в окне tolerance_pct от current_low."""
        if len(lows) < lookback:
            window = lows
        else:
            window = lows[-lookback:]
        tol = self.equal_lows_tolerance_pct
        count = 0
        for v in window:
            if abs(v - current_low) / max(current_low, 1e-10) <= tol:
                count += 1
        return count

    def _is_near_round_number(self, price: float) -> bool:
        """Проверить, близко ли число к round number (психологический уровень)."""
        if price <= 0:
            return False
        # Для BTC: $1000, $500, $100. Для XRP: $0.1, $0.01. Универсально — log-шаг.
        for step in [0.001, 0.01, 0.1, 1, 10, 100, 1000]:
            if step < 0.01:
                continue  # слишком мелкий шаг
            ratio = abs(price / step - round(price / step))
            if ratio < 0.01:  # в пределах 1% от round
                return True
        return False

    def _calculate_wick_ratio(self, bar: pd.Series) -> float:
        """Для LONG hunt: lower_wick / body. Чем больше, тем сильнее sweep."""
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        body = abs(c - o)
        if body < 1e-10:
            body = abs(h - l) * 0.1  # doji — минимальное тело
        lower_wick = min(o, c) - l
        return lower_wick / max(body, 1e-10)

    def _detect_bullish_hunt(self, df: pd.DataFrame, last: int) -> Optional[Dict[str, Any]]:
        """Обнаружить bullish liquidity hunt на последнем баре.
        Возвращает dict с деталями setup или None."""
        if last < self.swing_lookback + 5:
            return None

        bar = df.iloc[last]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        vols = df["volume"].values
        price = c

        # 1. Find swing low
        swing_idx, swing_low = self._find_swing_low(lows, self.swing_lookback)
        if swing_low == float('inf'):
            return None

        # 2. Hunt: wick пробивает swing low
        sweep_dist = (swing_low - l) / swing_low
        if sweep_dist < self.sweep_threshold:
            return None

        # 3. Close back inside (выше swing low)
        if c <= swing_low:
            return None

        # 4. Wick quality
        wick_ratio = self._calculate_wick_ratio(bar)
        if wick_ratio < self.wick_body_min_ratio:
            return None

        # 5. Volume spike
        vol_avg = float(np.mean(vols[max(0, last-20):last]))
        if vol_avg <= 0:
            return None
        vol_spike = vols[last] / vol_avg
        if vol_spike < self.min_volume_spike:
            return None

        # 6. Session filter
        bar_time = df.index[last]
        if hasattr(bar_time, "hour"):
            hour_utc = bar_time.hour
        else:
            hour_utc = 12  # default
        if not (self.session_start_utc <= hour_utc < self.session_end_utc):
            return None

        # 7. Equal lows bonus
        eql_count = self._find_equal_lows(lows, swing_low, lookback=100)
        has_eql = eql_count >= self.min_equal_lows_count

        # 8. Round number bonus
        near_round = self._is_near_round_number(swing_low)

        # 9. SL = low of wick - 0.5% (за воскy)
        sl_price = l * 0.995
        sl_pct = (price - sl_price) / price * 100

        # 10. TP = recent swing high (последние swing_lookback баров)
        recent_high = float(np.max(highs[max(0, last-self.swing_lookback):last]))
        if recent_high <= price:
            tp_pct = 1.5
        else:
            tp_pct = (recent_high - price) / price * 100

        # 11. R:R check
        if sl_pct <= 0 or tp_pct / sl_pct < self.min_rr_ratio:
            return None

        # Score
        score = 0.65
        reasons = [
            f"bullish_hunt:sw={sweep_dist*100:.2f}%,wr={wick_ratio:.1f},vol={vol_spike:.1f}x"
        ]
        if has_eql:
            score += 0.10
            reasons.append(f"eql={eql_count}")
        if near_round:
            score += 0.05
            reasons.append("near_round")

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

    def _detect_bearish_hunt(self, df: pd.DataFrame, last: int) -> Optional[Dict[str, Any]]:
        """Аналогично для SHORT: wick выше swing high + close обратно вниз."""
        if last < self.swing_lookback + 5:
            return None

        bar = df.iloc[last]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        highs = df["high"].values
        lows = df["low"].values
        vols = df["volume"].values
        price = c

        # Find swing high
        window = highs[-self.swing_lookback-1:-1]
        swing_idx = int(np.argmax(window))
        swing_high = float(window[swing_idx])
        if swing_high == float('-inf'):
            return None

        # Hunt: wick выше swing high
        sweep_dist = (h - swing_high) / swing_high
        if sweep_dist < self.sweep_threshold:
            return None

        # Close back inside (ниже swing high)
        if c >= swing_high:
            return None

        # Wick quality (upper wick)
        body = abs(c - o)
        if body < 1e-10:
            body = abs(h - l) * 0.1
        upper_wick = h - max(o, c)
        wick_ratio = upper_wick / max(body, 1e-10)
        if wick_ratio < self.wick_body_min_ratio:
            return None

        # Volume spike
        vol_avg = float(np.mean(vols[max(0, last-20):last]))
        if vol_avg <= 0:
            return None
        vol_spike = vols[last] / vol_avg
        if vol_spike < self.min_volume_spike:
            return None

        # Session filter
        bar_time = df.index[last]
        if hasattr(bar_time, "hour"):
            hour_utc = bar_time.hour
        else:
            hour_utc = 12
        if not (self.session_start_utc <= hour_utc < self.session_end_utc):
            return None

        # Equal highs
        eql_count = 0
        tol = self.equal_lows_tolerance_pct
        window_full = highs[-100:]
        for v in window_full:
            if abs(v - swing_high) / max(swing_high, 1e-10) <= tol:
                eql_count += 1
        has_eqh = eql_count >= self.min_equal_lows_count

        # Round number
        near_round = self._is_near_round_number(swing_high)

        # SL = high of wick + 0.5%
        sl_price = h * 1.005
        sl_pct = (sl_price - price) / price * 100

        # TP = recent swing low
        recent_low = float(np.min(lows[max(0, last-self.swing_lookback):last]))
        if recent_low >= price:
            tp_pct = 1.5
        else:
            tp_pct = (price - recent_low) / price * 100

        if sl_pct <= 0 or tp_pct / sl_pct < self.min_rr_ratio:
            return None

        score = 0.65
        reasons = [f"bearish_hunt:sw={sweep_dist*100:.2f}%,wr={wick_ratio:.1f},vol={vol_spike:.1f}x"]
        if has_eqh:
            score += 0.10
            reasons.append(f"eqh={eql_count}")
        if near_round:
            score += 0.05
            reasons.append("near_round")

        return {
            "side": "SHORT",
            "score": min(0.95, score),
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "swing_high": swing_high,
            "sweep_dist_pct": sweep_dist * 100,
            "wick_ratio": wick_ratio,
            "vol_spike": vol_spike,
            "eqh_count": eql_count,
            "near_round": near_round,
            "reasons": reasons,
            "hour_utc": hour_utc,
        }

    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        if df.empty or len(df) < 60:
            return self._hold("insufficient_data", 0.0, {})

        last = len(df) - 1
        bullish = self._detect_bullish_hunt(df, last)
        bearish = self._detect_bearish_hunt(df, last)

        # Берём более сильный setup
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
            "regime": "liquidity_hunt",
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
