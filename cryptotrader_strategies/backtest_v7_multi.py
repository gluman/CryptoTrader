#!/usr/bin/env python3
"""Backtest v7 (martingale + trailing) на 9 парах (без BSBUSDT) за 3 мес."""
import os, sys, json
from datetime import datetime, timezone
sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')
import numpy as np
from cryptotrader_strategies.clone_5_v7 import Clone5V7Strategy, run_v7_backtest

# Топ-пары (БЕЗ BSBUSDT — исключён)
PAIRS = ["TONUSDT", "DOGEUSDT", "SUIUSDT", "NEARUSDT", "SOLUSDT", "ZECUSDT", "ONDOUSDT", "LITUSDT", "HYPEUSDT"]
NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)
START_3M = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
SIZE = 5.0

c = Clone5V7Strategy()
c.params.symbols = PAIRS
print(f"=== v7 Backtest: 9 pairs (no BSBUSDT), 3-month, base ${SIZE}/trade ===", flush=True)
print(f"Martingale: [1.0, 1.5, 2.0] | Max losses: 3 | Daily limit: -$5 | Exposure cap: $30", flush=True)
print(f"Trailing: enabled, step 0.3%, interval 15min", flush=True)
print("="*100, flush=True)

results = run_v7_backtest(c, START_3M, NOW, size_usdt=SIZE, symbols=PAIRS)

print(f"\n{'Pair':<14} {'Tr':>4} {'WR%':>5} {'PF':>5} {'PnL$':>7} {'MaxDD$':>7} {'Step':>4} {'Blackout':>8}", flush=True)
print("-"*70, flush=True)

total_pnl = 0
total_dd = 0
all_trades = []
for sym, r in results.items():
    total_pnl += r['pnl_usd']
    total_dd = max(total_dd, r['max_dd_usd'])
    all_trades.extend(r['trades'])
    print(f"{sym:<14} {r['n']:>4} {r['wr_pct']:>5.1f} {r['pf']:>5.2f} ${r['pnl_usd']:>+6.2f} ${r['max_dd_usd']:>5.2f} {r['final_step']:>4} {r['blackouts']:>8}", flush=True)

# Aggregate metrics
if all_trades:
    pnls = [t.pnl_dollar for t in all_trades]
    wins = sum(1 for p in pnls if p > 0)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 10.0
    print("-"*70, flush=True)
    print(f"{'TOTAL':<14} {len(all_trades):>4} {wins/len(pnls)*100:>5.1f} {pf:>5.2f} ${sum(pnls):>+6.2f}", flush=True)

    # Exit reason breakdown
    exit_stats = {}
    for t in all_trades:
        er = t.exit_reason
        if er not in exit_stats:
            exit_stats[er] = {'n': 0, 'wins': 0, 'pnl': 0}
        exit_stats[er]['n'] += 1
        exit_stats[er]['pnl'] += t.pnl_dollar
        if t.pnl_dollar > 0:
            exit_stats[er]['wins'] += 1
    print(f"\n=== EXIT REASONS (v7) ===", flush=True)
    for er, st in sorted(exit_stats.items(), key=lambda x: -x[1]['n']):
        wr = st['wins']/st['n']*100 if st['n'] > 0 else 0
        print(f"  {er:<20}  n={st['n']:>3}  WR={wr:>5.1f}%  PnL=${st['pnl']:+.3f}", flush=True)

# Save
out = '/tmp/clones_backtest/v7_multi_pair_20260606.json'
os.makedirs(os.path.dirname(out), exist_ok=True)
serial = {sym: {k: v for k, v in r.items() if k != 'trades'} for sym, r in results.items()}
with open(out, 'w') as f:
    json.dump(serial, f, indent=2, default=str)
print(f"\n✓ Saved to {out}", flush=True)
