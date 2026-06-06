#!/usr/bin/env python3
"""
Backtest + Forward-test runner для всех 4 клонов.

Использование:
    # Backtest (исторические данные, -30d ... -16d)
    python cryptotrader_strategies/run_backtest.py --mode back

    # Forward test (последние 14d, simulated $5/position)
    python cryptotrader_strategies/run_backtest.py --mode forward

    # Конкретные клоны
    python cryptotrader_strategies/run_backtest.py --mode back --strategies clone0_current clone1_low_risk

    # Конкретные пары
    python cryptotrader_strategies/run_backtest.py --mode back --pairs BTCUSDT ETHUSDT

Метрики: trades, WR, avg_win%, avg_loss%, PF, total_pnl_$, max_dd_%, sharpe_like
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2

# === Setup ===
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cryptotrader_strategies import get_all_strategies

DB_CONFIG = dict(
    host="192.168.0.149", port=5432, database="cryptotrader",
    user="cryptotrader", password="cryptotrader123",
)

# Все поддерживаемые символы
ALL_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "TONUSDT", "AVAXUSDT", "ADAUSDT",
]

# Маппинг TF в строковые значения, которые в БД
TF_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60", "4h": "240", "1d": "1"}


@dataclass
class TradeResult:
    """Результат одной сделки."""
    symbol: str
    side: str               # LONG/SHORT
    entry_idx: int
    entry_price: float
    entry_time: pd.Timestamp
    exit_idx: int
    exit_price: float
    exit_time: pd.Timestamp
    exit_reason: str        # TP / SL / TRAILING / SIGNAL_EXIT / MAX_HOLD
    pnl_pct: float          # gross, без fees
    pnl_pct_net: float      # с учётом fees
    pnl_dollar: float       # на size_usdt
    size_usdt: float
    holding_minutes: int
    strategy: str
    details: Dict = field(default_factory=dict)


# === Data loading ===

def load_ohlcv(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Загрузить OHLCV из БД за [start, end). Возвращает df с колонками ohlc+volume, index=timestamp."""
    tf_db = TF_MAP.get(timeframe, timeframe)
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        query = """
            SELECT timestamp, open, high, low, close, volume
            FROM ohlcv_raw
            WHERE exchange = 'bybit' AND symbol = %s AND timeframe = %s
              AND timestamp >= %s AND timestamp < %s
            ORDER BY timestamp ASC
        """
        df = pd.read_sql(query, conn, params=(symbol, tf_db, start, end), parse_dates=["timestamp"])
    finally:
        conn.close()
    if df.empty:
        return df
    df = df.set_index("timestamp").sort_index()
    # Гарантируем float
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


# === Backtester ===

def run_backtest_for_strategy(
    strategy,
    start: datetime,
    end: datetime,
    size_usdt: float = 5.0,
    decision_every_n_bars: int = 1,
    max_bars_per_trade: Optional[int] = None,
) -> List[TradeResult]:
    """Запустить backtest одной стратегии по всем её парам.

    Returns: список TradeResult.
    """
    trades: List[TradeResult] = []
    tf_minutes = _tf_to_minutes(strategy.params.timeframe)
    if max_bars_per_trade is None:
        # default: 4ч макс или пока не выйдет
        max_bars_per_trade = int(strategy.params.max_hold_minutes / tf_minutes)

    for symbol in strategy.params.symbols:
        df = load_ohlcv(symbol, strategy.params.timeframe, start, end)
        if df.empty or len(df) < 60:
            continue

        strategy.logger.info(f"  {symbol}: {len(df)} bars ({df.index[0]} → {df.index[-1]})")

        i = 60  # warmup для индикаторов
        while i < len(df) - 1:
            history = df.iloc[:i + 1]
            decision = strategy.decide(history, symbol)

            sig = decision.get("signal")
            conf = decision.get("confidence", 0.0)
            sl_pct = decision.get("stop_loss_pct", 0.0)
            tp_pct = decision.get("take_profit_pct", 0.0)
            side = decision.get("side")

            # Фильтр: открываем только если сигнал BUY/SELL И conf >= min_confidence
            if sig in ("BUY", "SELL") and conf >= strategy.params.min_confidence and side and sl_pct > 0:
                entry_price = float(df.iloc[i]["close"])
                entry_time = df.index[i]

                # Считаем бары до выхода
                trade = _simulate_trade(
                    df=df, entry_idx=i, side=side, entry_price=entry_price,
                    entry_time=entry_time, sl_pct=sl_pct, tp_pct=tp_pct,
                    max_bars=max_bars_per_trade, tf_minutes=tf_minutes,
                    strategy=strategy, size_usdt=size_usdt,
                )
                trades.append(trade)
                i = trade.exit_idx + 1  # skip to next bar after exit
            else:
                i += decision_every_n_bars

    return trades


def _simulate_trade(
    df: pd.DataFrame, entry_idx: int, side: str, entry_price: float,
    entry_time: pd.Timestamp, sl_pct: float, tp_pct: float, max_bars: int,
    tf_minutes: int, strategy, size_usdt: float,
) -> TradeResult:
    """Симулировать одну сделку от entry_idx до выхода (TP/SL/trailing/max_hold)."""
    is_long = (side == "LONG")
    if is_long:
        sl_price = entry_price * (1 - sl_pct / 100)
        tp_price = entry_price * (1 + tp_pct / 100)
    else:
        sl_price = entry_price * (1 + sl_pct / 100)
        tp_price = entry_price * (1 - tp_pct / 100)

    trailing_activated = False
    exit_idx = entry_idx
    exit_price = entry_price
    exit_reason = "MAX_HOLD"
    cur_sl = sl_price
    cur_tp = tp_price
    minutes_held = 0

    for j in range(entry_idx + 1, min(len(df), entry_idx + 1 + max_bars)):
        bar = df.iloc[j]
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        minutes_held += tf_minutes

        # === Trailing stop (проверяем каждые trailing_interval_min) ===
        if strategy.params.trailing_enabled and minutes_held >= strategy.params.trailing_interval_min:
            new_sl, cur_tp, activated = strategy.apply_trailing(
                side, entry_price, high, low, cur_sl, cur_tp, minutes_held
            )
            if activated:
                cur_sl = new_sl
                trailing_activated = True
                strategy.logger.debug(f"  trailing: SL → {cur_sl:.6f}")

        # === Проверка SL/TP (на одном баре — TP приоритет, иначе 5m SL всегда бьёт) ===
        if is_long:
            hit_sl = low <= cur_sl
            hit_tp = high >= cur_tp
        else:
            hit_sl = high >= cur_sl
            hit_tp = low <= cur_tp

        if hit_sl and hit_tp:
            exit_idx, exit_price, exit_reason = j, cur_tp, "TP"
            break
        if hit_sl:
            exit_idx, exit_price, exit_reason = j, cur_sl, "SL" + ("_TRAILING" if trailing_activated else "")
            break
        if hit_tp:
            exit_idx, exit_price, exit_reason = j, cur_tp, "TP"
            break
    else:
        # Цикл завершился без break → max_hold
        exit_idx = min(len(df) - 1, entry_idx + max_bars)
        exit_price = float(df.iloc[exit_idx]["close"])
        exit_reason = "MAX_HOLD"

    # === Расчёт P&L ===
    if is_long:
        pnl_pct = (exit_price - entry_price) / entry_price * 100
    else:
        pnl_pct = (entry_price - exit_price) / entry_price * 100

    # Fees: round-trip 2 × fee_pct
    pnl_pct_net = pnl_pct - 2 * strategy.params.fee_pct
    pnl_dollar = size_usdt * pnl_pct_net / 100  # margin $5, pnl % от margin

    return TradeResult(
        symbol=df.iloc[entry_idx].name if hasattr(df.iloc[entry_idx], 'name') else "unknown",
        side=side, entry_idx=entry_idx, entry_price=entry_price, entry_time=entry_time,
        exit_idx=exit_idx, exit_price=exit_price, exit_time=df.index[exit_idx],
        exit_reason=exit_reason, pnl_pct=pnl_pct, pnl_pct_net=pnl_pct_net,
        pnl_dollar=pnl_dollar, size_usdt=size_usdt, holding_minutes=minutes_held,
        strategy=strategy.params.name,
    )


# === Метрики ===

def compute_metrics(trades: List[TradeResult]) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0, "wr_pct": 0.0, "pnl_usd": 0.0, "pf": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "max_dd_pct": 0.0}
    n = len(trades)
    wins = [t for t in trades if t.pnl_pct_net > 0]
    losses = [t for t in trades if t.pnl_pct_net <= 0]
    wr = len(wins) / n * 100
    total_pnl = sum(t.pnl_dollar for t in trades)
    gross_win = sum(t.pnl_dollar for t in wins) if wins else 0.0
    gross_loss = abs(sum(t.pnl_dollar for t in losses)) if losses else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (10.0 if gross_win > 0 else 0.0)
    avg_win = (sum(t.pnl_pct_net for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_pct_net for t in losses) / len(losses)) if losses else 0.0

    # Equity curve & max drawdown
    equity = np.cumsum([t.pnl_dollar for t in trades])
    running_max = np.maximum.accumulate(equity) if len(equity) > 0 else np.array([0.0])
    dd = (equity - running_max) if len(equity) > 0 else np.array([0.0])
    max_dd = float(dd.min()) if len(dd) > 0 else 0.0

    # Sharpe-like: avg(pnl) / std(pnl)
    pnls = [t.pnl_dollar for t in trades]
    sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(n) if n > 1 and np.std(pnls) > 0 else 0.0

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "wr_pct": round(wr, 1),
        "pnl_usd": round(total_pnl, 2),
        "pf": round(pf, 2),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "avg_hold_min": round(np.mean([t.holding_minutes for t in trades]), 1) if trades else 0.0,
        "max_dd_usd": round(max_dd, 2) if trades else 0.0,
        "sharpe_like": round(sharpe, 2) if trades else 0.0,
        "tp_hits": sum(1 for t in trades if t.exit_reason == "TP"),
        "sl_hits": sum(1 for t in trades if t.exit_reason.startswith("SL")),
        "max_hold_exits": sum(1 for t in trades if t.exit_reason == "MAX_HOLD"),
    }


# === Helpers ===

def _tf_to_minutes(tf: str) -> int:
    return {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}.get(tf, 5)


# === Main ===

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["back", "forward", "both"], default="both")
    parser.add_argument("--strategies", nargs="+", default=None,
                        help="Имена стратегий (default: all)")
    parser.add_argument("--size", type=float, default=5.0, help="Position size USDT")
    parser.add_argument("--out-dir", default="/tmp/clones_backtest",
                        help="Директория для результатов")
    parser.add_argument("--fee", type=float, default=None,
                        help="Override fee %% per side (default: from strategy params)")
    args = parser.parse_args()

    Path(args.out_dir).mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    # Back: -30d to -16d (14 days)
    back_start = now - pd.Timedelta(days=30)
    back_end = now - pd.Timedelta(days=16)
    # Forward: -14d to now
    fwd_start = now - pd.Timedelta(days=14)
    fwd_end = now

    all_strats = get_all_strategies()
    if args.strategies:
        all_strats = {k: v for k, v in all_strats.items() if k in args.strategies}

    print(f"=== CryptoTrader 4-Clone Backtest ===")
    print(f"Mode: {args.mode}  |  Size: ${args.size}  |  Strategies: {list(all_strats.keys())}")
    print(f"Back:    {back_start} → {back_end}")
    print(f"Forward: {fwd_start} → {fwd_end}")
    print()

    summary = {"back": {}, "forward": {}}

    for name, strat in all_strats.items():
        if args.fee is not None:
            strat.params.fee_pct = args.fee
        print(f"\n{'='*60}")
        print(f"  {name}  (TF={strat.params.timeframe}, conf≥{strat.params.min_confidence}, "
              f"SL={strat.params.sl_pct}%, TP={strat.params.tp_pct}%, "
              f"trailing={'yes' if strat.params.trailing_enabled else 'no'})")
        print(f"  Symbols: {strat.params.symbols}")
        print(f"{'='*60}")

        # === BACK ===
        if args.mode in ("back", "both"):
            print(f"\n  [BACK] {back_start.date()} → {back_end.date()}")
            t0 = time.time()
            back_trades = run_backtest_for_strategy(strat, back_start, back_end, size_usdt=args.size)
            back_metrics = compute_metrics(back_trades)
            back_metrics = {k: v for k, v in back_metrics.items()}  # ensure keys
            elapsed = time.time() - t0
            print(f"  Trades: {back_metrics['trades']}  WR: {back_metrics['wr_pct']}%  "
                  f"PnL: ${back_metrics['pnl_usd']}  PF: {back_metrics['pf']}  "
                  f"MaxDD: ${back_metrics['max_dd_usd']}  Sharpe: {back_metrics['sharpe_like']}  "
                  f"[{elapsed:.1f}s]")
            print(f"  AvgWin: {back_metrics['avg_win_pct']}%  AvgLoss: {back_metrics['avg_loss_pct']}%  "
                  f"AvgHold: {back_metrics['avg_hold_min']}min")
            print(f"  Exits: TP={back_metrics['tp_hits']} SL={back_metrics['sl_hits']} "
                  f"MaxHold={back_metrics['max_hold_exits']}")
            summary["back"][name] = {"metrics": back_metrics, "trades": [vars(t) for t in back_trades]}

        # === FORWARD ===
        if args.mode in ("forward", "both"):
            print(f"\n  [FWD]  {fwd_start.date()} → {fwd_end.date()}")
            t0 = time.time()
            fwd_trades = run_backtest_for_strategy(strat, fwd_start, fwd_end, size_usdt=args.size)
            fwd_metrics = compute_metrics(fwd_trades)
            fwd_metrics = {k: v for k, v in fwd_metrics.items()}  # ensure keys
            elapsed = time.time() - t0
            print(f"  Trades: {fwd_metrics['trades']}  WR: {fwd_metrics['wr_pct']}%  "
                  f"PnL: ${fwd_metrics['pnl_usd']}  PF: {fwd_metrics['pf']}  "
                  f"MaxDD: ${fwd_metrics['max_dd_usd']}  Sharpe: {fwd_metrics['sharpe_like']}  "
                  f"[{elapsed:.1f}s]")
            print(f"  AvgWin: {fwd_metrics['avg_win_pct']}%  AvgLoss: {fwd_metrics['avg_loss_pct']}%  "
                  f"AvgHold: {fwd_metrics['avg_hold_min']}min")
            print(f"  Exits: TP={fwd_metrics['tp_hits']} SL={fwd_metrics['sl_hits']} "
                  f"MaxHold={fwd_metrics['max_hold_exits']}")
            summary["forward"][name] = {"metrics": fwd_metrics, "trades": [vars(t) for t in fwd_trades]}

    # Save
    out_file = Path(args.out_dir) / f"summary_{args.mode}_{now.strftime('%Y%m%d_%H%M')}.json"
    # Convert timestamps to strings for JSON
    for mode_data in summary.values():
        for strat_data in mode_data.values():
            for t in strat_data.get("trades", []):
                if isinstance(t.get("entry_time"), pd.Timestamp):
                    t["entry_time"] = t["entry_time"].isoformat()
                if isinstance(t.get("exit_time"), pd.Timestamp):
                    t["exit_time"] = t["exit_time"].isoformat()
    with open(out_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n\nResults saved: {out_file}")

    # === Comparison table ===
    if args.mode == "both":
        mode_key = "back"  # for the per-strategy block print, default to back
    else:
        mode_key = args.mode
    for tbl_mode in (["back", "forward"] if args.mode == "both" else [args.mode]):
        print(f"\n{'='*80}")
        print(f"  COMPARISON TABLE  ({tbl_mode})")
        print(f"{'='*80}")
        print(f"  {'Strategy':<30} {'Trades':>7} {'WR%':>6} {'PnL$':>8} {'PF':>6} {'MaxDD$':>7} {'Sharpe':>7}")
        print(f"  {'-'*30} {'-'*7} {'-'*6} {'-'*8} {'-'*6} {'-'*7} {'-'*7}")
        for name in all_strats.keys():
            m = summary[tbl_mode].get(name, {}).get("metrics", {})
            if m:
                print(f"  {name:<30} {m['trades']:>7} {m['wr_pct']:>6} {m['pnl_usd']:>8} "
                      f"{m['pf']:>6} {m['max_dd_usd']:>7} {m['sharpe_like']:>7}")


if __name__ == "__main__":
    main()
