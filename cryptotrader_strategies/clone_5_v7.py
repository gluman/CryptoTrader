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
        name="clone5_v7_market_maker_martingale",
        timeframe="5m",
        symbols=["TONUSDT", "DOGEUSDT", "SUIUSDT"],
        min_confidence=0.50,
        sl_pct=0.5,
        tp_pct=2.0,
        # === V7: Trailing ENABLED (был отключён в v6) ===
        trailing_enabled=True,
        trailing_interval_min=15,  # проверять каждые 15 мин
        trailing_step_pct=0.30,  # активируется при +0.3% (раньше 0.5%)
        max_hold_minutes=180,
        fee_pct=0.055,
    )

    def __init__(self, logger=None):
        super().__init__(logger)
        # Per-pair martingale state
        self.martingale_state: Dict[str, MartingaleState] = {}

    def _get_state(self, symbol: str) -> MartingaleState:
        if symbol not in self.martingale_state:
            self.martingale_state[symbol] = MartingaleState()
        return self.martingale_state[symbol]

    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """V7 decide: проверяет safety filters ПЕРЕД генерацией сигнала."""
        if df.empty or len(df) < 60:
            return self._hold("insufficient_data", 0.0, {})

        self._apply_per_pair_params(symbol)
        last = len(df) - 1
        current_ts = df.index[last]

        # === Safety check: blackout after max consecutive losses ===
        state = self._get_state(symbol)
        if state.is_blackout(current_ts):
            return self._hold(f"blackout_until_{state.blackout_until}", 0.0, {"state": "blackout"})

        # === Safety check: daily loss limit ===
        if state.daily_loss_exceeded():
            return self._hold("daily_loss_limit_exceeded", 0.0, {"state": "daily_loss_exceeded", "daily_pnl": state.daily_pnl_today})

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
            "regime": "v7_hunt_martingale",
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
                "martingale_step": state.current_step,
                "martingale_size": state.get_position_size(5.0),
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
