#!/usr/bin/env python3
"""
Backtest Clone5 v2 (ICT) vs v1 (grid 36). Запуск 3-мес.
"""
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/andy/CryptoTrader')
from cryptotrader_strategies.clone_5_v2_ict import Clone5V2Strategy
from cryptotrader_strategies.clone_5_market_maker import Clone5MarketMakerStrategy
from cryptotrader_strategies.run_backtest import run_backtest_for_strategy, compute_metrics

SYMBOLS = ["TONUSDT", "DOGEUSDT", "SUIUSDT"]
NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)
START_3M = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
SIZE = 5.0
PAIR_SIZE = SIZE / len(SYMBOLS)

print("Clone5 v1 vs v2 (ICT) — 3-month backtest", flush=True)
print(f"Symbols: {SYMBOLS}, Size: ${SIZE} (${PAIR_SIZE:.2f}/pair)", flush=True)
print(f"Period: {START_3M.date()} → {NOW.date()}", flush=True)
print("="*100, flush=True)

results = {}
for strat_name, strat_cls in [("v1_market_maker", Clone5MarketMakerStrategy), ("v2_ict", Clone5V2Strategy)]:
    print(f"\n--- {strat_name} ---", flush=True)
    print(f"{'Symbol':>10} {'Tr':>4} {'WR%':>5} {'PF':>5} {'PnL$':>6} {'AvgW%':>7} {'AvgL%':>7} {'Sh':>5}", flush=True)
    print("-"*60, flush=True)
    period_trades = []
    for sym in SYMBOLS:
        c = strat_cls()
        c.params.symbols = [sym]
        try:
            trades = run_backtest_for_strategy(c, START_3M, NOW, size_usdt=PAIR_SIZE)
            m = compute_metrics(trades)
            results[(strat_name, sym)] = m
            period_trades.extend(trades)
            print(f"{sym:>10} {m['trades']:>4} {m['wr_pct']:>5.1f} {m['pf']:>5.2f} ${m['pnl_usd']:>+5.2f} {m['avg_win_pct']:>+7.2f} {m['avg_loss_pct']:>+7.2f} {m['sharpe_like']:>5.2f}", flush=True)
        except Exception as e:
            print(f"{sym:>10} ERROR: {e}", flush=True)
    if period_trades:
        agg = compute_metrics(period_trades)
        print(f"  AGG: tr={agg['trades']} WR={agg['wr_pct']:.1f}% PF={agg['pf']:.2f} PnL=${agg['pnl_usd']:+.2f} MaxDD=${agg['max_dd_usd']:+.2f} Sh={agg['sharpe_like']:.2f}", flush=True)
        results[(strat_name, '__AGG__')] = agg

# Save
out_path = '/tmp/clones_backtest/clone5_v1_vs_v2_20260606.json'
os.makedirs(os.path.dirname(out_path), exist_ok=True)
serial = {f"{s}|{p}": m for (s, p), m in results.items()}
with open(out_path, 'w') as f:
    json.dump(serial, f, indent=2, default=str)
print(f"\n✓ Saved to {out_path}", flush=True)
