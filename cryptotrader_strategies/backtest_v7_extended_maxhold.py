#!/usr/bin/env python3
"""
Extended max_hold grid: v7a trailing-only + v7b martingale-only.
Сетка: 30, 60, 90, 120, 150, 180, 210, 240, 300, 360, 480, 720 мин (12 значений)
Период: 3 мес (2026-03-07 → 2026-06-07)
Пары: 28 (BSBUSDT excluded)
Size: $1/pos

Boss request 7 июня: добавить 120, 180, 210, 240+ (longer holds).
"""
from __future__ import annotations

import os, sys, json, time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import psycopg2

from cryptotrader_strategies.clone_5_v7 import (
    Clone5V7Strategy, PER_PAIR_EXPOSURE_CAP,
)
from cryptotrader_strategies.run_backtest import load_ohlcv, _simulate_trade, compute_metrics, TradeResult

DB = dict(host="192.168.0.149", port=5432, database="cryptotrader",
          user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"])
EXCLUDE = ["BSBUSDT"]
MAX_HOLDS = [30, 60, 90, 120, 150, 180, 210, 240, 300, 360, 480, 720]
VARIANTS = [
    ("v7a", True, False),    # trailing on, martingale off
    ("v7b", False, True),    # trailing off, martingale on
]


def get_pairs(min_bars=4000):
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, COUNT(*) FROM ohlcv_raw
        WHERE exchange='bybit' AND timeframe='5m'
        GROUP BY symbol HAVING COUNT(*) >= %s
    """, (min_bars,))
    pairs = [r[0] for r in cur.fetchall() if r[0] not in EXCLUDE]
    cur.close(); conn.close()
    return pairs


def run_one(strategy, start, end, size_usdt, symbols, max_hold_min, use_trailing, use_martingale):
    tf_minutes = 5
    max_bars = int(max_hold_min / tf_minutes)
    strategy.params.trailing_enabled = use_trailing

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

        trades = []
        i = 60
        while i < len(df) - 1:
            history = df.iloc[:i + 1]
            current_ts = history.index[-1]
            if state.is_blackout(current_ts): i += 1; continue
            if state.daily_loss_exceeded(): i += 1; continue
            decision = strategy.decide(history, symbol)
            sig = decision.get("signal")
            conf = decision.get("confidence", 0.0)
            sl_pct = decision.get("stop_loss_pct", 0.0)
            tp_pct = decision.get("take_profit_pct", 0.0)
            side = decision.get("side")
            if sig in ("BUY", "SELL") and conf >= strategy.params.min_confidence and side and sl_pct > 0:
                if use_martingale:
                    pos_size = state.get_position_size(size_usdt)
                    if pos_size > PER_PAIR_EXPOSURE_CAP:
                        pos_size = PER_PAIR_EXPOSURE_CAP
                else:
                    pos_size = size_usdt
                trade = _simulate_trade(
                    df=df, entry_idx=i, side=side,
                    entry_price=float(df.iloc[i]["close"]),
                    entry_time=current_ts, sl_pct=sl_pct, tp_pct=tp_pct,
                    max_bars=max_bars, tf_minutes=tf_minutes,
                    strategy=strategy, size_usdt=pos_size,
                )
                trades.append(trade)
                pnl = trade.pnl_dollar
                if pnl > 0: state.record_win(trade.exit_time, pnl)
                else: state.record_loss(trade.exit_time, pnl)
                i = trade.exit_idx + 1
            else:
                i += 1
        results[symbol] = (trades, compute_metrics(trades))
    return results


def aggregate(results):
    """Sum per-pair metrics into one aggregate."""
    all_trades = []
    for sym, (trades, m) in results.items():
        all_trades.extend(trades)
    agg = compute_metrics(all_trades)
    return agg, all_trades


def main():
    print("=== Extended max_hold grid: v7a + v7b × 12 holds ===", flush=True)
    print(f"Started: {datetime.now(timezone.utc).isoformat()}", flush=True)
    end = datetime(2026, 6, 7, 0, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
    size = 1.0
    symbols = get_pairs()
    print(f"Pairs: {len(symbols)}  Period: {start.date()} → {end.date()}  Size: ${size}", flush=True)
    print(f"max_hold grid: {MAX_HOLDS} min ({len(MAX_HOLDS)} values)", flush=True)
    print(f"Variants: {len(VARIANTS)} (v7a trailing, v7b martingale)", flush=True)
    print(f"Total runs: {len(MAX_HOLDS) * len(VARIANTS)} (~{len(MAX_HOLDS) * len(VARIANTS) * 5} min)", flush=True)
    print("=" * 100, flush=True)

    all_data = {}  # {(variant, mh): {aggregate, per_pair, elapsed}}
    grand_start = time.time()

    for v_name, use_t, use_m in VARIANTS:
        print(f"\n##### VARIANT: {v_name} (trailing={use_t}, martingale={use_m}) #####", flush=True)
        for mh in MAX_HOLDS:
            t0 = time.time()
            c = Clone5V7Strategy()
            c.params.symbols = symbols
            res = run_one(c, start, end, size, symbols, mh, use_t, use_m)
            elapsed = time.time() - t0
            agg, all_trades = aggregate(res)
            all_data[(v_name, mh)] = {
                "aggregate": agg,
                "per_pair": {s: {k: v for k, v in m.items() if k != "trades"} for s, (tr, m) in res.items()},
                "trade_list": all_trades,
                "elapsed": round(elapsed, 1),
            }
            print(f"  [{v_name}|{mh:>3}min] tr={agg['trades']:>3}  WR={agg['wr_pct']:>5}%  "
                  f"PF={agg['pf']:>5}  PnL=${agg['pnl_usd']:>+6.2f}  "
                  f"MaxDD=${agg['max_dd_usd']:>5.2f}  Sharpe={agg['sharpe_like']:>5}  "
                  f"TP={agg['tp_hits']:>2}  SL={agg['sl_hits']:>3}  MH={agg['max_hold_exits']:>3}  "
                  f"[{elapsed:.0f}s]", flush=True)

    grand_elapsed = time.time() - grand_start
    print(f"\n=== GRAND TOTAL: {grand_elapsed:.0f}s ===", flush=True)

    # === COMPARISON TABLES ===
    print("\n" + "=" * 110)
    print("v7a (trailing) — aggregate by max_hold")
    print("=" * 110)
    print(f"{'max_hold':>9} {'Trades':>7} {'WR%':>6} {'PnL$':>8} {'PF':>6} {'MaxDD$':>7} {'Sharpe':>7} {'TP':>4} {'SL':>4} {'MH':>4}")
    print("-" * 110)
    for mh in MAX_HOLDS:
        a = all_data[("v7a", mh)]["aggregate"]
        print(f"{mh:>7}min {a['trades']:>7} {a['wr_pct']:>6} ${a['pnl_usd']:>+7.2f} "
              f"{a['pf']:>6} ${a['max_dd_usd']:>6.2f} {a['sharpe_like']:>7} "
              f"{a['tp_hits']:>4} {a['sl_hits']:>4} {a['max_hold_exits']:>4}")

    print("\n" + "=" * 110)
    print("v7b (martingale) — aggregate by max_hold")
    print("=" * 110)
    print(f"{'max_hold':>9} {'Trades':>7} {'WR%':>6} {'PnL$':>8} {'PF':>6} {'MaxDD$':>7} {'Sharpe':>7} {'TP':>4} {'SL':>4} {'MH':>4}")
    print("-" * 110)
    for mh in MAX_HOLDS:
        a = all_data[("v7b", mh)]["aggregate"]
        print(f"{mh:>7}min {a['trades']:>7} {a['wr_pct']:>6} ${a['pnl_usd']:>+7.2f} "
              f"{a['pf']:>6} ${a['max_dd_usd']:>6.2f} {a['sharpe_like']:>7} "
              f"{a['tp_hits']:>4} {a['sl_hits']:>4} {a['max_hold_exits']:>4}")

    # Best per variant
    print("\n" + "=" * 80)
    print("BEST max_hold by variant (by PF)")
    print("=" * 80)
    for v_name, _, _ in VARIANTS:
        best_mh = max(MAX_HOLDS, key=lambda m: all_data[(v_name, m)]["aggregate"]["pf"])
        a = all_data[(v_name, best_mh)]["aggregate"]
        print(f"  {v_name}: best at {best_mh}min — PF={a['pf']}, PnL=${a['pnl_usd']:+.2f}, "
              f"WR={a['wr_pct']}%, Sharpe={a['sharpe_like']}")

    # Save
    out_dir = Path("/tmp/clones_backtest")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"v7_extended_maxhold_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    serial = {}
    for (v_name, mh), data in all_data.items():
        serial[f"{v_name}_mh{mh}"] = {
            "variant": v_name,
            "max_hold_min": mh,
            "aggregate": data["aggregate"],
            "elapsed": data["elapsed"],
            "per_pair": data["per_pair"],
        }
    with open(out_file, "w") as fh:
        json.dump(serial, fh, indent=2, default=str)
    print(f"\n✓ Saved: {out_file}", flush=True)


if __name__ == "__main__":
    main()
