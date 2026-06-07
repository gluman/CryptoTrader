#!/usr/bin/env python3
"""
max_hold grid search для v7b (martingale-only, без trailing).
Проверяем влияние max_hold_minutes на edge.

Сетка: [30, 60, 90, 120, 180, 240] минут
Период: 3 мес (2026-03-07 → 2026-06-07)
Пары: 28 (BSBUSDT excluded)
Size: $1/pos
"""
from __future__ import annotations

import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import psycopg2

from cryptotrader_strategies.clone_5_v7 import (
    Clone5V7Strategy,
    PER_PAIR_EXPOSURE_CAP,
)
from cryptotrader_strategies.run_backtest import load_ohlcv, _simulate_trade, compute_metrics, TradeResult

DB = dict(
    host="192.168.0.149", port=5432, database="cryptotrader",
    user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"],
)
EXCLUDE = ["BSBUSDT"]
MAX_HOLDS = [30, 60, 90, 120, 180, 240]


def get_pairs(min_bars: int = 4000):
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, COUNT(*) FROM ohlcv_raw
        WHERE exchange='bybit' AND timeframe='5m'
        GROUP BY symbol HAVING COUNT(*) >= %s
    """, (min_bars,))
    pairs = []
    for r in cur.fetchall():
        if r[0] not in EXCLUDE:
            pairs.append(r[0])
    cur.close()
    conn.close()
    return pairs


def run_max_hold(strategy: Clone5V7Strategy, start, end, size_usdt: float,
                 symbols, max_hold_min: int):
    """v7b martingale-only, NO trailing, with custom max_hold_minutes."""
    tf_minutes = 5
    max_bars_per_trade = int(max_hold_min / tf_minutes)
    # В v7 trailing_enabled=True по умолчанию — принудительно OFF
    strategy.params.trailing_enabled = False

    results = {}
    for symbol in symbols:
        df = load_ohlcv(symbol, strategy.params.timeframe, start, end)
        if df.empty or len(df) < 60:
            continue

        state = strategy._get_state(symbol)
        state.consecutive_losses = 0
        state.current_step = 0
        state.daily_pnl_today = 0.0
        state.daily_date = None
        state.blackout_until = None

        trades: list[TradeResult] = []
        i = 60
        while i < len(df) - 1:
            history = df.iloc[:i + 1]
            current_ts = history.index[-1]

            if state.is_blackout(current_ts):
                i += 1; continue
            if state.daily_loss_exceeded():
                i += 1; continue

            decision = strategy.decide(history, symbol)
            sig = decision.get("signal")
            conf = decision.get("confidence", 0.0)
            sl_pct = decision.get("stop_loss_pct", 0.0)
            tp_pct = decision.get("take_profit_pct", 0.0)
            side = decision.get("side")

            if sig in ("BUY", "SELL") and conf >= strategy.params.min_confidence and side and sl_pct > 0:
                pos_size = state.get_position_size(size_usdt)
                if pos_size > PER_PAIR_EXPOSURE_CAP:
                    pos_size = PER_PAIR_EXPOSURE_CAP
                trade = _simulate_trade(
                    df=df, entry_idx=i, side=side, entry_price=float(df.iloc[i]["close"]),
                    entry_time=current_ts, sl_pct=sl_pct, tp_pct=tp_pct,
                    max_bars=max_bars_per_trade, tf_minutes=tf_minutes,
                    strategy=strategy, size_usdt=pos_size,
                )
                trades.append(trade)
                pnl = trade.pnl_dollar
                if pnl > 0:
                    state.record_win(trade.exit_time, pnl)
                else:
                    state.record_loss(trade.exit_time, pnl)
                i = trade.exit_idx + 1
            else:
                i += 1

        m = compute_metrics(trades)
        m["symbol"] = symbol
        results[symbol] = m
    return results


def main():
    print("=== max_hold grid: v7b martingale-only ===", flush=True)
    print(f"Started: {datetime.now(timezone.utc).isoformat()}", flush=True)
    end = datetime(2026, 6, 7, 0, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
    size = 1.0
    symbols = get_pairs()
    print(f"Pairs: {len(symbols)}  Period: {start.date()} → {end.date()}  Size: ${size}", flush=True)
    print(f"max_hold grid: {MAX_HOLDS} min", flush=True)
    print("=" * 100, flush=True)

    all_results = {}
    for mh in MAX_HOLDS:
        print(f"\n>>> max_hold={mh}min ...", flush=True)
        t0 = time.time()
        c = Clone5V7Strategy()
        c.params.symbols = symbols
        res = run_max_hold(c, start, end, size, symbols, mh)
        elapsed = time.time() - t0
        all_trades = []
        for sym, m in res.items():
            # m is the metrics dict from compute_metrics; it has 'trades' as the count, not the list.
            # Reconstruct via per-pair 'trades' field if present, else skip
            if isinstance(m.get('trades'), list):
                all_trades.extend(m['trades'])
            elif 'all_trades' in res:
                all_trades.extend(res['all_trades'])
        agg = compute_metrics(all_trades)
        print(f"    [{mh:>3} min] trades={agg['trades']:>3}  WR={agg['wr_pct']:>5}%  "
              f"PF={agg['pf']:>5}  PnL=${agg['pnl_usd']:>+6.2f}  "
              f"MaxDD=${agg['max_dd_usd']:>5.2f}  Sharpe={agg['sharpe_like']:>5}  "
              f"TP={agg['tp_hits']:>2}  SL={agg['sl_hits']:>3}  MH={agg['max_hold_exits']:>3}  "
              f"[{elapsed:.0f}s]", flush=True)
        all_results[mh] = {"aggregate": agg, "per_pair": res, "elapsed": round(elapsed, 1),
                           "all_trades": all_trades}

    # === COMPARISON ===
    print("\n" + "=" * 100)
    print("max_hold GRID COMPARISON (v7b, martingale-only, 28 pairs, 3 months)")
    print("=" * 100)
    print(f"{'max_hold':>9} {'Trades':>7} {'WR%':>6} {'PnL$':>8} {'PF':>6} {'MaxDD$':>7} {'Sharpe':>7} {'TP':>4} {'SL':>4} {'MH':>4}")
    print("-" * 100)
    for mh in MAX_HOLDS:
        a = all_results[mh]["aggregate"]
        print(f"{mh:>7}min {a['trades']:>7} {a['wr_pct']:>6} ${a['pnl_usd']:>+7.2f} "
              f"{a['pf']:>6} ${a['max_dd_usd']:>6.2f} {a['sharpe_like']:>7} "
              f"{a['tp_hits']:>4} {a['sl_hits']:>4} {a['max_hold_exits']:>4}")

    # Per-pair detail for top values
    print("\n" + "=" * 110)
    print("PER-PAIR PF (max_hold sweep on v7b)")
    print("=" * 110)
    print(f"{'Pair':<14} " + " ".join([f"{f'mh{mh:>3}':>10}" for mh in MAX_HOLDS]) + f"  {'best_mh':>8}  {'best_PF':>7}")
    print("-" * 110)
    for sym in symbols:
        row = [sym]
        pfs = []
        for mh in MAX_HOLDS:
            try:
                pf = all_results[mh]["per_pair"][sym]["pf"]
            except KeyError:
                pf = 0.0
            row.append(pf)
            pfs.append(pf)
        best_idx = int(np.argmax(pfs)) if pfs else 0
        print(f"{sym:<14} " + " ".join([f"{pf:>10.2f}" for pf in pfs]) + f"  {MAX_HOLDS[best_idx]:>5}min  {pfs[best_idx]:>7.2f}")

    # Verdict
    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    best_mh = max(MAX_HOLDS, key=lambda m: all_results[m]["aggregate"]["pf"])
    best = all_results[best_mh]["aggregate"]
    print(f"Best max_hold by PF: {best_mh}min → PF={best['pf']}, PnL=${best['pnl_usd']:+.2f}, "
          f"WR={best['wr_pct']}%, Sharpe={best['sharpe_like']}")
    worst_mh = min(MAX_HOLDS, key=lambda m: all_results[m]["aggregate"]["pf"])
    worst = all_results[worst_mh]["aggregate"]
    print(f"Worst max_hold by PF: {worst_mh}min → PF={worst['pf']}, PnL=${worst['pnl_usd']:+.2f}")

    # Save
    out_dir = Path("/tmp/clones_backtest")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"v7b_maxhold_grid_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    serial = {}
    for mh, data in all_results.items():
        serial[f"mh_{mh}"] = {
            "max_hold_min": mh,
            "aggregate": data["aggregate"],
            "elapsed": data["elapsed"],
            "per_pair": {s: {k: v for k, v in m.items() if k != "trades"}
                         for s, m in data["per_pair"].items()},
        }
    with open(out_file, "w") as fh:
        json.dump(serial, fh, indent=2, default=str)
    print(f"\n✓ Saved: {out_file}", flush=True)


if __name__ == "__main__":
    main()
