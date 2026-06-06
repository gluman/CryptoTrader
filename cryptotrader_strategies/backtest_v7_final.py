#!/usr/bin/env python3
"""V7 final OOS с per-pair tuned params (2026-06-07)."""
import os, sys
from datetime import datetime, timezone
sys.path.insert(0, '/home/andy/CryptoTrader')
from cryptotrader_strategies.clone_5_v7 import Clone5V7Strategy, run_v7_backtest

NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)
START = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)

SYMBOLS = ["SUIUSDT", "NEARUSDT", "SOLUSDT", "LITUSDT", "WLDUSDT", "TONUSDT", "DOGEUSDT", "ADAUSDT"]
SIZE = 5.0
PAIR_SIZE = SIZE / 2  # 2 max concurrent

print(f"=== v7 FINAL OOS (per-pair tuned) ===", flush=True)
print(f"Period: {START.date()} → {NOW.date()} | Pos: $2.50/pos (2 concurrent = $5 total)", flush=True)
print("="*100, flush=True)

c = Clone5V7Strategy()
c.params.symbols = SYMBOLS
results = run_v7_backtest(c, START, NOW, size_usdt=PAIR_SIZE, symbols=SYMBOLS)

print(f"\n{'Pair':<12} {'Tr':>4} {'WR%':>5} {'PF':>5} {'PnL$':>7} {'MaxDD$':>7} {'Step':>4} {'Blackout':>8}", flush=True)
print("-"*70, flush=True)

total_pnl = 0
total_trades = 0
for sym, r in sorted(results.items(), key=lambda x: -x[1].get('pf', 0)):
    pf = r['pf'] if r['pf'] != 10.0 or r['n'] > 0 else 0.0
    print(f"{sym:<12} {r['n']:>4} {r['wr_pct']:>5.1f} {pf:>5.2f} ${r['pnl_usd']:>+6.2f} ${r['max_dd_usd']:>5.2f} {r['final_step']:>4} {r['blackouts']:>8}", flush=True)
    total_pnl += r['pnl_usd']
    total_trades += r['n']

print("-"*70, flush=True)
print(f"{'TOTAL':<12} {total_trades:>4}     {' ':>5} ${total_pnl:>+6.2f}", flush=True)
print(f"\nNote: real concurrent = 2, so divide position size accordingly", flush=True)
