#!/usr/bin/env python3
"""
Clone5 v7 — Market Maker / Liquidity Hunt + Martingale + Trailing Stop + Safety Filters.

НОВОЕ В V7:
  - Martingale levels [1.0, 1.5, 2.0] (3 ступени) — после каждого убытка +50% size
  - Max 3 consecutive losses per pair → cooldown 24h (через state tracker)
  - Daily loss limit -$5 per pair
  - Per-pair exposure cap $30
  - Trailing stop активируется при +0.3% (был отключён в v6)
  - Trailing distance: 0.2% (бьёт раньше, чем SL)
  - После 3 убытков подряд на паре — STOP 24h (state.pair_blackout)

Использование:
  from clone_5_v7 import Clone5V7Strategy
  c = Clone5V7Strategy()
  c.run_backtest_with_martingale(...)  # симулирует martingale на trades
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .base_strategy import (
    BaseStrategy,
    StrategyParams,
)
from .clone_5_v6 import Clone5V6Strategy, PER_PAIR_PARAMS


# === Martingale configuration ===
MARTINGALE_LEVELS = [1.0, 1.5, 2.0]  # 3 ступени
MAX_CONSECUTIVE_LOSSES = 3  # после 3 убытков подряд — stop 24h
DAILY_LOSS_LIMIT = -5.0  # $5 per pair per day
COOLDOWN_HOURS = 24  # blackout после max losses
PER_PAIR_EXPOSURE_CAP = 30.0  # max $ on one pair at once (3 pairs × step 2 × $5 = $30)


# === Per-pair tuned params (from grid_v7_per_pair 2026-06-07) ===
# R19 (19.06.2026): sweep ослаблен ×0.5, wick ×0.75 (с 0.8→0.6) — пропорционально
# релаксации дефолтов. min_volume_spike здесь не задаётся (наследует дефолт 1.0 из v6 R19).
# Бэкап: clone_5_v7.py.bak.relax.20260619_150944
PER_PAIR_PARAMS_V7 = {
    "NEARUSDT": {"swing_lookback": 120, "sweep_threshold": 0.0025, "wick_body_min_ratio": 0.6},
    "SOLUSDT":  {"swing_lookback": 50,  "sweep_threshold": 0.0015, "wick_body_min_ratio": 0.6},
    "LITUSDT":  {"swing_lookback": 50,  "sweep_threshold": 0.004,  "wick_body_min_ratio": 0.6},
    "WLDUSDT":  {"swing_lookback": 30,  "sweep_threshold": 0.004,  "wick_body_min_ratio": 0.6},
}


@dataclass
class MartingaleState:
    """Per-pair state для martingale + safety filters."""
    consecutive_losses: int = 0
    last_trade_pnl: float = 0.0
    last_trade_ts: Optional[pd.Timestamp] = None
    daily_pnl_today: float = 0.0
    daily_date: Optional[str] = None
    blackout_until: Optional[pd.Timestamp] = None
    current_step: int = 0  # 0 = base, 1 = ×1.5, 2 = ×2.0

    def reset_step(self):
        self.current_step = 0

    def increment_step(self):
        if self.current_step < len(MARTINGALE_LEVELS) - 1:
            self.current_step += 1

    def record_loss(self, ts: pd.Timestamp, pnl: float):
        self.consecutive_losses += 1
        self.last_trade_pnl = pnl
        self.last_trade_ts = ts
        self._update_daily(ts, pnl)
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.blackout_until = ts + pd.Timedelta(hours=COOLDOWN_HOURS)

    def record_win(self, ts: pd.Timestamp, pnl: float):
        self.consecutive_losses = 0
        self.last_trade_pnl = pnl
        self.last_trade_ts = ts
        self.reset_step()
        self._update_daily(ts, pnl)

    def _update_daily(self, ts: pd.Timestamp, pnl: float):
        date_str = ts.date().isoformat() if hasattr(ts, 'date') else str(ts)[:10]
        if self.daily_date != date_str:
            self.daily_date = date_str
            self.daily_pnl_today = 0.0
        self.daily_pnl_today += pnl

    def is_blackout(self, ts: pd.Timestamp) -> bool:
        if self.blackout_until is None:
            return False
        return ts < self.blackout_until

    def daily_loss_exceeded(self) -> bool:
        return self.daily_pnl_today <= DAILY_LOSS_LIMIT

    def get_position_size(self, base_size: float) -> float:
        """Возвращает размер позиции с учётом martingale step."""
        mult = MARTINGALE_LEVELS[min(self.current_step, len(MARTINGALE_LEVELS) - 1)]
        return base_size * mult

    def should_increment_step(self) -> bool:
        return self.consecutive_losses > 0 and self.current_step < len(MARTINGALE_LEVELS) - 1


class Clone5V7Strategy(Clone5V6Strategy):
    """Clone5 v7: V6 + Trailing Stop + Martingale-ready (state в отдельном dict)."""

    PARAMS = StrategyParams(
        name="clone5_v7_trailing_only",
        timeframe="5m",  # R16 (17.06): откат 15m → 5m (PF 1.89 vs 1.02)
        symbols=["SUIUSDT", "NEARUSDT", "SOLUSDT", "LITUSDT", "DOGEUSDT", "ADAUSDT",
                  "AKEUSDT", "SOXLUSDT", "ZECUSDT", "ENAUSDT",
                  "APTUSDT", "TAOUSDT", "AVAXUSDT", "AAVEUSDT",
                  "BCHUSDT", "DYDXUSDT", "AXSUSDT", "MAGICUSDT", "KSMUSDT"],
        min_confidence=0.50,
        # R16: baseline 5m params. Grid search на полном окне 148д уточнит.
        # R22 (08.07.2026, Босс): SL/TP расширены для fee-survival на $15 notional.
        #   • Было 0.5/2.0% → fee round-trip ~0.10% всё равно ест gross в большинстве сделок.
        #   • Стало 1.2/3.5% (R:R 1:2.9) — gross ≥0.5%, fee лезет в sl_pct buffer.
        # Срабатывает: этот default перебивает fallback в clone5_multi_runner.py:614/747
        # ТОЛЬКО когда v7a.calculate_setup() возвращает именно эти default'ы;
        # если LLM передаёт llm_sl_pct — runner берёт LLM-override.
        sl_pct=1.2,
        tp_pct=3.5,
        # === V7a: Trailing ON (главный edge, см. ablation 2026-06-07) ===
        trailing_enabled=True,
        trailing_interval_min=15,  # проверять каждые 15 мин
        # R16 (17.06): 0.3% → 0.2% (rolling WF 18 windows показал 0.2% как winner).
        # Mean PF=3.70 vs 1.97 у 0.3%, total PnL +50.67% vs +10.40% за 148д.
        # Edge режимный: прибыльно W14-W17 (bull market), убыточно W1-W5 (flat).
        # Trailing OFF = 12/18 profitable, но total -1.56% (breakeven).
        trailing_step_pct=0.20,
        # === MAX_HOLD = 30min (extended grid 12 значений 7 июня 2026) ===
        # Best PF/Sharpe, +40% PnL vs default 180, +46% Sharpe, тот же edge.
        # Martingale v7b убыточен на всех max_hold (PF<1) — отключён, см. open_position().
        # [Fix 17.07.2026 Босс] max_hold 30→15 мин: регрессия 06-12.07 показала,
        # 14% trades закрылись по max_hold с pnl ≈ 0 (timeout). Быстрый выход экономит fees.
        max_hold_minutes=15,
        fee_pct=0.055,
    )

    def __init__(self, logger=None):
        super().__init__(logger)
        # Per-pair martingale state
        self.martingale_state: Dict[str, MartingaleState] = {}
        # R17 (18.06.2026): volatility regime detector.
        # BB width market_avg > 1.0% → vol_high (trailing works),
        # else vol_low (trailing off, avoid chop losses).
        # Set externally by clone5_multi_runner.compute_vol_regime() before scan.
        self._vol_regime: str = "unknown"  # "high" | "low" | "unknown"
        self._vol_bb_width: float = 0.0    # last computed BB width (%)

    def _get_state(self, symbol: str) -> MartingaleState:
        if symbol not in self.martingale_state:
            self.martingale_state[symbol] = MartingaleState()
        return self.martingale_state[symbol]

    def set_vol_regime(self, regime: str, bb_width: float) -> None:
        """Set volatility regime (called by runner before decide())."""
        self._vol_regime = regime
        self._vol_bb_width = bb_width
        # R17: if regime is low, force trailing OFF regardless of PARAMS default.
        if regime == "low":
            self.params.trailing_enabled = False
        elif regime == "high":
            self.params.trailing_enabled = True

    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """V7 decide: проверяет safety filters ПЕРЕД генерацией сигнала."""
        if df.empty or len(df) < 60:
            return self._hold("insufficient_data", 0.0, {})

        self._apply_per_pair_params(symbol)
        # Apply per-pair v7 tuned params (overrides v6 per-pair)
        if symbol in PER_PAIR_PARAMS_V7:
            p = PER_PAIR_PARAMS_V7[symbol]
            self.swing_lookback = p.get('swing_lookback', self.swing_lookback)
            self.sweep_threshold = p.get('sweep_threshold', self.sweep_threshold)
            self.wick_body_min_ratio = p.get('wick_body_min_ratio', self.wick_body_min_ratio)
        last = len(df) - 1
        current_ts = df.index[last]  # type: ignore[assignment]

        # ────────────────────────────────────────────────────────────────────
        # HOUR FILTER (17.07.2026): Торгуем только 09-18 MSK.
        # ЗАЧЕМ: Регрессия 36 сделок 06-12.07 показала — ночные/утренние часы
        # (00-08, 22-23) убыточны: −$0.89 суммарно. Пик прибыли 09-13 MSK: +$0.30.
        # Ограничиваем входы в окно высокой ликвидности (Европа + US premarket).
        # 09-18 MSK = 06-15 UTC.
        # ────────────────────────────────────────────────────────────────────
        current_ts_p: pd.Timestamp = pd.Timestamp(current_ts)
        if current_ts_p.tzinfo is None:
            current_ts_p = current_ts_p.tz_localize('UTC')
        else:
            current_ts_p = current_ts_p.tz_convert('UTC')
        hour_utc = current_ts_p.hour
        if not (6 <= hour_utc < 15):  # 06:00 UTC = 09:00 MSK, 15:00 UTC = 18:00 MSK
            return self._hold("off_hours", 0.0, {"last": last, "hour_utc": hour_utc})

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
            # R9 FIX: переименовано v7_hunt_martingale → v7_hunt (мартингейл отключён, misleading)
            "regime": "v7_hunt",
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
                # R17 (18.06.2026): regime info для мониторинга.
                "vol_regime": self._vol_regime,
                "vol_bb_width": round(self._vol_bb_width, 4),
                # R18 (18.06.2026): gap-filter diagnostics (D3/E2/B4/N5)
                "is_engulfing": setup.get("is_engulfing", False),
                "trend_alignment": setup.get("trend_alignment", "neutral"),
                "trend_penalty": setup.get("trend_penalty", 0.0),
                "mc_bonus": setup.get("mc_bonus", 0.0),
                "bar_return_pct": round(setup.get("bar_return_pct", 0.0), 3),
                # R9 FIX: martingale_step/size — всегда 0/base×1.0 в live, убраны из details
                # чтобы не мусорить отчётность. MartingaleState нужен только для backtest.
                # "martingale_step": self._get_state(symbol).current_step,
                # "martingale_size": self._get_state(symbol).get_position_size(5.0),
            },
        }

    def apply_trailing(self, side: str, entry: float, high: float, low: float,
                       current_sl: float, current_tp: float, minutes_held: int) -> Tuple[float, float, bool]:
        """
        V7 trailing: активируется при +0.3% (step), подтягивает SL на расстоянии step от peak.
        Переопределяет BaseStrategy.apply_trailing.
        """
        activation_pct = self.PARAMS.trailing_step_pct
        is_long = (side == "LONG")
        new_sl = current_sl
        activated = False

        if is_long:
            profit_pct = (high - entry) / entry * 100
            if minutes_held >= self.PARAMS.trailing_interval_min and profit_pct >= activation_pct:
                # Подтягиваем SL на расстоянии step_pct от текущего high
                new_sl = high * (1 - activation_pct / 100)
                if new_sl > current_sl:
                    activated = True
        else:
            profit_pct = (entry - low) / entry * 100
            if minutes_held >= self.PARAMS.trailing_interval_min and profit_pct >= activation_pct:
                new_sl = low * (1 + activation_pct / 100)
                if new_sl < current_sl:
                    activated = True
        return new_sl, current_tp, activated


# === Martingale + safety backtest simulation ===
def run_v7_backtest(strategy: Clone5V7Strategy, start, end, size_usdt=5.0, symbols=None):
    """
    Запустить V7 backtest с мартингейл + safety filters.
    Возвращает dict {symbol: {trades, equity_curve, final_pnl, max_dd}}.
    """
    from cryptotrader_strategies.run_backtest import load_ohlcv
    from cryptotrader_strategies.run_backtest import _simulate_trade
    if symbols is None:
        symbols = strategy.params.symbols

    tf_minutes = 5
    max_bars_per_trade = int(strategy.params.max_hold_minutes / tf_minutes)

    results = {}
    for symbol in symbols:
        df = load_ohlcv(symbol, strategy.params.timeframe, start, end)
        if df.empty or len(df) < 60:
            continue
        state = strategy._get_state(symbol)
        trades = []
        i = 60
        while i < len(df) - 1:
            history = df.iloc[:i + 1]
            current_ts = history.index[-1]

            # Check blackout / daily limit
            if state.is_blackout(current_ts):
                i += 1
                continue
            if state.daily_loss_exceeded():
                i += 1
                continue

            decision = strategy.decide(history, symbol)
            sig = decision.get("signal")
            conf = decision.get("confidence", 0.0)
            sl_pct = decision.get("stop_loss_pct", 0.0)
            tp_pct = decision.get("take_profit_pct", 0.0)
            side = decision.get("side")

            if sig in ("BUY", "SELL") and conf >= strategy.params.min_confidence and side and sl_pct > 0:
                # === Martingale position size ===
                pos_size = state.get_position_size(size_usdt)
                # Per-pair exposure cap
                if pos_size > PER_PAIR_EXPOSURE_CAP:
                    pos_size = PER_PAIR_EXPOSURE_CAP
                trade = _simulate_trade(
                    df=df, entry_idx=i, side=side, entry_price=float(df.iloc[i]["close"]),
                    entry_time=current_ts, sl_pct=sl_pct, tp_pct=tp_pct,
                    max_bars=max_bars_per_trade, tf_minutes=tf_minutes,
                    strategy=strategy, size_usdt=pos_size,
                )
                trades.append(trade)
                # === Update martingale state ===
                pnl = trade.pnl_dollar
                if pnl > 0:
                    state.record_win(trade.exit_time, pnl)
                else:
                    state.record_loss(trade.exit_time, pnl)
                i = trade.exit_idx + 1
            else:
                i += 1

        # Compute metrics
        if trades:
            pnls = [t.pnl_dollar for t in trades]
            equity = np.cumsum(pnls)
            max_dd = float((equity - np.maximum.accumulate(equity)).min()) if len(equity) > 0 else 0
            wins = sum(1 for t in trades if t.pnl_dollar > 0)
            wr = wins / len(trades) * 100
            gross_win = sum(t.pnl_dollar for t in trades if t.pnl_dollar > 0)
            gross_loss = abs(sum(t.pnl_dollar for t in trades if t.pnl_dollar <= 0))
            pf = gross_win / gross_loss if gross_loss > 0 else (10.0 if gross_win > 0 else 0.0)
            results[symbol] = {
                'trades': trades,
                'n': len(trades),
                'wr_pct': wr,
                'pnl_usd': sum(pnls),
                'max_dd_usd': max_dd,
                'pf': pf,
                'final_step': state.current_step,
                'blackouts': 1 if state.blackout_until else 0,
            }
        else:
            results[symbol] = {
                'trades': [], 'n': 0, 'wr_pct': 0, 'pnl_usd': 0, 'max_dd_usd': 0,
                'pf': 0, 'final_step': 0, 'blackouts': 0,
            }
    return results
