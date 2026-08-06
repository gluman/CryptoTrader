#!/usr/bin/env python3
"""
Clone5 v9 — Counter-Trend Long (CTL).

ЧТО ЭТО. Единственная конфигурация, пережившая честную проверку вне выборки
(04.08.2026): покупка отскока после свипа ликвидности вниз, когда часовой тренд
направлен ВНИЗ. То есть вход против старшего тренда — ровно то, что блокировал
фильтр R25.

ПРОВЕРКА (5m, комиссия 0.11% round-trip, вход по open следующего бара):
  • 26 боевых пар, 90 дней: n=2056, exp +0.152%/сделку, WR 54.6%, PF 1.20, t=3.63;
  • holdout (30% периода, в подборе не участвовал): +0.144%, 22/26 пар в плюсе;
  • 25 ПОСТОРОННИХ пар, параметры зафиксированы до теста: n=1722, exp +0.097%,
    PF 1.13, t=2.19 — перенос подтверждён;
  • на боевом определении тренда (SMA20/SMA50 + flat): +0.136%, t=2.90.

ЗЕРКАЛЬНЫЙ КОНТРОЛЬ (почему это не случайность):
  против тренда + LONG  → +0.152   ← наш случай
  по тренду     + LONG  → −0.059
  против тренда + SHORT → −0.117
  по тренду     + SHORT → +0.015

ЧЕСТНАЯ ОГОВОРКА: на посторонних парах 3 пары из 24 дали 107% всего плюса
(без них ≈ ноль). Эдж тонкий и неравномерный — размер позиции держать минимальным,
результат оценивать не раньше чем через несколько сотен сделок.

УСЛОВИЯ ВХОДА (все обязательны, порядок как в проверенном коде):
  1. swing_low = min(low) за последние `lookback` баров ДО текущего;
  2. свип: (swing_low − low) / swing_low >= sweep_threshold;
  3. возврат: close > swing_low;
  4. нижняя тень: (min(open, close) − low) / body >= wick_body_min_ratio;
  5. объём: volume / средний за 20 баров >= min_volume_spike;
  6. сессия: session_start <= час UTC < session_end;
  7. не-FOMO: |close − open| / open <= 2%;
  8. часовой тренд = "down" (вход ПРОТИВ тренда);
  9. только LONG — шортовая версия убыточна, см. зеркальный контроль.

SL/TP/max_hold — персональные для каждой пары, из config/v9_per_pair_params.json
(подобраны при зафиксированных условиях входа; при отсутствии пары — дефолты ниже).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from .clone_5_v8 import Clone5V8Strategy


# === Условия входа (проверены вне выборки, менять только с новой проверкой) ===
CTL_LOOKBACK = 80
CTL_SWEEP_THRESHOLD = 0.0005
CTL_WICK_BODY_MIN = 0.8
CTL_MIN_VOLUME_SPIKE = 0.5
CTL_SESSION_START_UTC = 8
CTL_SESSION_END_UTC = 20
CTL_FOMO_MAX_BAR_RETURN_PCT = 2.0

# === Дефолты выхода, если пары нет в конфиге ===
CTL_DEFAULT_SL = 2.5
CTL_DEFAULT_TP = 2.5
CTL_DEFAULT_MAX_HOLD_MIN = 360

V9_PARAMS_PATH = Path("/home/andy/CryptoTrader_main/config/v9_per_pair_params.json")


def load_v9_params() -> Dict[str, Dict[str, float]]:
    """Персональные SL/TP/max_hold по парам. При любой проблеме — пустой dict (дефолты)."""
    try:
        if V9_PARAMS_PATH.exists():
            data = json.loads(V9_PARAMS_PATH.read_text())
            return data.get("params", {})
    except Exception as e:
        print(f"⚠️  v9: не удалось прочитать {V9_PARAMS_PATH.name}: {e}", flush=True)
    return {}


class Clone5V9Strategy(Clone5V8Strategy):
    """Counter-Trend Long. decide() переопределён целиком — CHOCH, score-система
    и trend-фильтр R25 из старших версий НЕ участвуют."""

    def __init__(self):
        super().__init__()
        self.v9_params = load_v9_params()
        self.params.name = "clone5_v9_ctl"

    def exit_params_for(self, symbol: str) -> Dict[str, float]:
        p = self.v9_params.get(symbol, {})
        return {
            "sl_pct": float(p.get("sl_pct", CTL_DEFAULT_SL)),
            "tp_pct": float(p.get("tp_pct", CTL_DEFAULT_TP)),
            "max_hold_min": int(p.get("max_hold_min", CTL_DEFAULT_MAX_HOLD_MIN)),
        }

    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        last = len(df) - 1
        if last < CTL_LOOKBACK + 5:
            return self._hold("v9: мало баров", 0.0, {"bars": len(df)})

        bar = df.iloc[last]
        o, h, l, c = (float(bar["open"]), float(bar["high"]),
                      float(bar["low"]), float(bar["close"]))
        lows = df["low"].values
        vols = df["volume"].values

        # 1-2. свип swing low
        swing_low = float(np.min(lows[last - CTL_LOOKBACK:last]))
        if swing_low <= 0:
            return self._hold("v9: некорректный swing_low", 0.0, {})
        sweep_dist = (swing_low - l) / swing_low
        if sweep_dist < CTL_SWEEP_THRESHOLD:
            return self._hold("v9: нет свипа", 0.0, {"sweep_dist_pct": sweep_dist * 100})

        # 3. возврат выше уровня
        if c <= swing_low:
            return self._hold("v9: нет возврата за уровень", 0.0, {"close": c, "swing_low": swing_low})

        # 4. нижняя тень
        body = abs(c - o)
        if body < 1e-10:
            body = abs(h - l) * 0.1
        wick_ratio = (min(o, c) - l) / max(body, 1e-10)
        if wick_ratio < CTL_WICK_BODY_MIN:
            return self._hold("v9: слабая нижняя тень", 0.0, {"wick_ratio": wick_ratio})

        # 5. объём
        vol_avg = float(np.mean(vols[max(0, last - 20):last]))
        if vol_avg <= 0:
            return self._hold("v9: нет объёма", 0.0, {})
        vol_spike = float(vols[last]) / vol_avg
        if vol_spike < CTL_MIN_VOLUME_SPIKE:
            return self._hold("v9: слабый объём", 0.0, {"vol_spike": vol_spike})

        # 6. сессия
        bar_time = df.index[last]
        hour_utc = bar_time.hour if hasattr(bar_time, "hour") else 12
        if not (CTL_SESSION_START_UTC <= hour_utc < CTL_SESSION_END_UTC):
            return self._hold("v9: вне сессии", 0.0, {"hour_utc": hour_utc})

        # 7. FOMO
        bar_ret = abs(c - o) / max(o, 1e-12) * 100
        if bar_ret > CTL_FOMO_MAX_BAR_RETURN_PCT:
            return self._hold("v9: FOMO-бар", 0.0, {"bar_return_pct": bar_ret})

        # 8. вход ТОЛЬКО против часового тренда
        trend = self._get_trend_for(symbol)
        if trend != "down":
            return self._hold("v9: тренд не down (нужен counter-trend)", 0.0, {"trend_1h": trend})

        exits = self.exit_params_for(symbol)
        return {
            "signal": "BUY",
            "side": "LONG",
            "confidence": 0.70,          # фиксированная: отбор делают условия, не score
            "stop_loss_pct": exits["sl_pct"],
            "take_profit_pct": exits["tp_pct"],
            "max_hold_minutes": exits["max_hold_min"],
            "score": 0.70,
            "regime": getattr(self, "_vol_regime", "unknown"),
            "reasoning": (
                f"v9 CTL: свип {sweep_dist*100:.2f}% под swing_low {swing_low:.6f}, "
                f"возврат close {c:.6f}, тень {wick_ratio:.2f}, объём {vol_spike:.2f}x, "
                f"час {hour_utc} UTC, 1h-тренд down → LONG против тренда"
            ),
            "details": {
                "strategy": "v9_ctl",
                "sweep_dist_pct": sweep_dist * 100,
                "swing_low": swing_low,
                "wick_ratio": wick_ratio,
                "vol_spike": vol_spike,
                "hour_utc": hour_utc,
                "trend_1h": trend,
                "sl_pct": exits["sl_pct"],
                "tp_pct": exits["tp_pct"],
                "max_hold_min": exits["max_hold_min"],
            },
        }


def create_v9_strategy() -> Clone5V9Strategy:
    return Clone5V9Strategy()
