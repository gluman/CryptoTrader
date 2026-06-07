#!/usr/bin/env python3
"""
OOS TEST: v7a trailing-only на 6 длинных парах за последние 2 недели (2026-05-24 → 2026-06-07).

OOS = out-of-sample: backtest v7a на данных, которые НЕ использовались в основном ablation.
Основной тест был 3 мес (2026-03-07 → 2026-06-07). OOS — последние 14 дней (04-06 — 07-06).
Пары с полной историей 138д: SOL/BTC/DOGE/XRP/TON/ETH.
Size: $1/pos.

Цель: проверить, что v7a держит edge out-of-sample (не overfit на 3-мес выборку).
"""
from __future__ import annotations

import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

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

# 6 длинных пар с 138д данных (исключаем BTC/ETH — они не в v7 list, но для OOS теста на чистоту)
# Лучше оставить 4 из v7 symbol list, которые имеют 138д: SOL, DOGE, XRP, TON
OOS_PAIRS = ["SOLUSDT", "DOGEUSDT", "XRPUSDT", "TONUSDT"]
OOS_DAYS = 14


def run_oos(strategy: Clone5V7Strategy, start, end, size_usdt, symbols):
    """v7a trailing-only, no martingale."""
    tf_minutes = 5
    max_bars_per_trade = int(strategy.params.max_hold_minutes / tf_minutes)
    strategy.params.trailing_enabled = True  # v7a ON

    results = {}
    for symbol in symbols:
        df = load_ohlcv(symbol, strategy.params.timeframe, start, end)
        if df.empty or len(df) < 60:
            print(f"  ⚠ {symbol}: insufficient data ({len(df)} bars)", flush=True)
            continue

        # Reset martingale state (martingale OFF anyway)
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
                # Martingale OFF — base size
                pos_size = size_usdt
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
        m["all_trades"] = trades
        results[symbol] = m
    return results


def main():
    print("=== v7a OOS TEST: 4 long-history pairs × 14d ===", flush=True)
    print(f"Started: {datetime.now(timezone.utc).isoformat()}", flush=True)
    end = datetime(2026, 6, 7, 0, 0, 0, tzinfo=timezone.utc)
    start = end - pd if False else datetime(2026, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
    size = 1.0
    print(f"Period: {start.date()} → {end.date()}  (OOS last {OOS_DAYS} days)", flush=True)
    print(f"Pairs: {OOS_PAIRS}", flush=True)
    print(f"Strategy: v7a trailing-only, NO martingale, $1/pos", flush=True)
    print("=" * 100, flush=True)

    t0 = time.time()
    c = Clone5V7Strategy()
    c.params.symbols = OOS_PAIRS
    res = run_oos(c, start, end, size, OOS_PAIRS)
    elapsed = time.time() - t0

    # Aggregate
    all_trades = []
    for sym, m in res.items():
        all_trades.extend(m.get("all_trades", []))
    agg = compute_metrics(all_trades)

    print(f"\n=== AGGREGATE (v7a OOS, 4 pairs × 14d, $1/pos) ===", flush=True)
    print(f"Trades: {agg['trades']}  WR: {agg['wr_pct']}%  PF: {agg['pf']}", flush=True)
    print(f"PnL: ${agg['pnl_usd']:+.2f}  MaxDD: ${agg['max_dd_usd']:.2f}  Sharpe: {agg['sharpe_like']}", flush=True)
    print(f"Exits: TP={agg['tp_hits']}  SL={agg['sl_hits']}  MaxHold={agg['max_hold_exits']}", flush=True)

    # Per-pair
    print("\n=== PER-PAIR ===")
    print(f"{'Pair':<14} {'Trades':>7} {'WR%':>6} {'PF':>6} {'PnL$':>8} {'MaxDD$':>8} {'TP':>4} {'SL':>4} {'MH':>4}")
    print("-" * 75)
    for sym in OOS_PAIRS:
        m = res.get(sym, {})
        if m.get("trades", 0) == 0 and not m.get("all_trades"):
            print(f"{sym:<14} 0 trades")
            continue
        pf = m["pf"]
        print(f"{sym:<14} {m['trades']:>7} {m['wr_pct']:>6} {pf:>6} ${m['pnl_usd']:>+7.2f} "
              f"${m['max_dd_usd']:>7.2f} {m['tp_hits']:>4} {m['sl_hits']:>4} {m['max_hold_exits']:>4}")

    # Trade list
    print("\n=== TRADE LIST (OOS) ===")
    for sym in OOS_PAIRS:
        m = res.get(sym, {})
        trades = m.get("all_trades", [])
        for t in trades:
            print(f"  {sym:<10} {t.side:<5} entry={t.entry_price:.5f} exit={t.exit_price:.5f} "
                  f"PnL=${t.pnl_dollar:+.3f} ({t.exit_reason})")

    # Comparison vs main 3mo
    print("\n=== OOS vs 3-MONTH COMPARISON ===")
    print(f"{'Pair':<14} {'OOS PF':>8} {'OOS PnL':>10}  {'3M PF':>8} {'3M PnL':>10}  {'Stable?':<8}")
    print("-" * 70)
    # Hardcoded 3m results from v7a ablation (28 pairs run)
    three_m = {
        "SOLUSDT": (7.83, 0.01),  # PF, PnL
        "DOGEUSDT": (1.04, 0.00),
        "XRPUSDT": (10.0, 0.00),  # only 2 trades, PF=10 (artifact)
        "TONUSDT": (1.37, 0.03),
    }
    for sym in OOS_PAIRS:
        oos_m = res.get(sym, {})
        oos_pf = oos_m.get("pf", 0)
        oos_pnl = oos_m.get("pnl_usd", 0)
        m3_pf, m3_pnl = three_m.get(sym, (0, 0))
        stable = "✓" if (oos_pf >= 0.7 * m3_pf and oos_pf > 0) or (oos_pf == 0 and m3_pf == 0) else "⚠"
        print(f"{sym:<14} {oos_pf:>8.2f} ${oos_pnl:>+9.2f}  {m3_pf:>8.2f} ${m3_pnl:>+9.2f}  {stable:<8}")

    # Verdict
    print("\n" + "=" * 70)
    print("OOS VERDICT")
    print("=" * 70)
    if agg['pf'] >= 1.0 and agg['pnl_usd'] > 0:
        print(f"✅ OOS HOLDS: PF={agg['pf']}, PnL=${agg['pnl_usd']:+.2f} — edge стабилен")
    elif agg['pf'] >= 0.7 and agg['pnl_usd'] > -0.5:
        print(f"⚠️ OOS SOFT: PF={agg['pf']}, PnL=${agg['pnl_usd']:+.2f} — слабый, нужен больший sample")
    else:
        print(f"❌ OOS BROKEN: PF={agg['pf']}, PnL=${agg['pnl_usd']:+.2f} — edge не out-of-sample")

    # Save
    out_dir = Path("/tmp/clones_backtest")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"v7a_oos_14d_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    serial = {
        "period": f"{start.date()} → {end.date()}",
        "pairs": OOS_PAIRS,
        "strategy": "v7a trailing-only, no martingale, $1/pos",
        "aggregate": agg,
        "per_pair": {s: {k: v for k, v in m.items() if k != "all_trades"} for s, m in res.items()},
        "elapsed_sec": round(elapsed, 1),
    }
    with open(out_file, "w") as fh:
        json.dump(serial, fh, indent=2, default=str)
    print(f"\n✓ Saved: {out_file}", flush=True)
    print(f"\nElapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
