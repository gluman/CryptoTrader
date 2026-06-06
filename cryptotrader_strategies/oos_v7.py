#!/usr/bin/env python3
"""OOS v7 (martingale + trailing): 4-week cross-validation."""
import os, sys, json
from datetime import datetime, timezone
sys.path.insert(0, '/home/andy/CryptoTrader')
from cryptotrader_strategies.clone_5_v7 import Clone5V7Strategy, run_v7_backtest

PAIRS = ["TONUSDT", "DOGEUSDT", "SUIUSDT", "NEARUSDT", "SOLUSDT", "ZECUSDT", "ONDOUSDT", "LITUSDT", "HYPEUSDT"]
PERIODS = [
    ("W1: 6.05-13.05", datetime(2026, 5, 6, tzinfo=timezone.utc), datetime(2026, 5, 13, tzinfo=timezone.utc)),
    ("W2: 13.05-20.05", datetime(2026, 5, 13, tzinfo=timezone.utc), datetime(2026, 5, 20, tzinfo=timezone.utc)),
    ("W3: 20.05-27.05", datetime(2026, 5, 20, tzinfo=timezone.utc), datetime(2026, 5, 27, tzinfo=timezone.utc)),
    ("W4: 27.05-3.06", datetime(2026, 5, 27, tzinfo=timezone.utc), datetime(2026, 6, 3, tzinfo=timezone.utc)),
    ("W5: 3.06-6.06", datetime(2026, 6, 3, tzinfo=timezone.utc), datetime(2026, 6, 6, 18, tzinfo=timezone.utc)),
]
SIZE = 5.0

print(f"=== v7 OOS Cross-Validation (4 weeks) ===", flush=True)
print(f"{'Period':<20} {'Tr':>4} {'WR%':>5} {'PF':>5} {'PnL$':>7} {'MaxDD$':>7}", flush=True)
print("-"*60, flush=True)

all_trades = []
all_pnl = 0
max_dd_period = 0
for name, start, end in PERIODS:
    c = Clone5V7Strategy()
    c.params.symbols = PAIRS
    try:
        results = run_v7_backtest(c, start, end, size_usdt=SIZE, symbols=PAIRS)
        # Aggregate
        period_pnl = sum(r['pnl_usd'] for r in results.values())
        period_n = sum(r['n'] for r in results.values())
        period_wins = sum(1 for sym_r in results.values() for t in sym_r['trades'] if t.pnl_dollar > 0)
        period_wr = period_wins/period_n*100 if period_n else 0
        gross_w = sum(t.pnl_dollar for sym_r in results.values() for t in sym_r['trades'] if t.pnl_dollar > 0)
        gross_l = abs(sum(t.pnl_dollar for sym_r in results.values() for t in sym_r['trades'] if t.pnl_dollar <= 0))
        pf = gross_w/gross_l if gross_l > 0 else 10.0
        max_dd = max(r['max_dd_usd'] for r in results.values()) if results else 0
        max_dd_period = max(max_dd_period, max_dd)
        all_trades.extend([t for r in results.values() for t in r['trades']])
        all_pnl += period_pnl
        print(f"{name:<20} {period_n:>4} {period_wr:>5.1f} {pf:>5.2f} ${period_pnl:>+5.2f} ${max_dd:>5.2f}", flush=True)
    except Exception as e:
        print(f"{name:<20} ERR: {e}", flush=True)

if all_trades:
    pnls = [t.pnl_dollar for t in all_trades]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p <= 0))
    pf = gross_w/gross_l if gross_l > 0 else 10.0
    print("-"*60, flush=True)
    print(f"{'AGG':<20} {len(pnls):>4} {wins/len(pnls)*100:>5.1f} {pf:>5.2f} ${sum(pnls):>+5.2f} ${max_dd_period:>5.2f}", flush=True)
    # Exit reasons
    exit_stats = {}
    for t in all_trades:
        er = t.exit_reason
        if er not in exit_stats:
            exit_stats[er] = {'n': 0, 'wins': 0, 'pnl': 0}
        exit_stats[er]['n'] += 1
        exit_stats[er]['pnl'] += t.pnl_dollar
        if t.pnl_dollar > 0:
            exit_stats[er]['wins'] += 1
    print(f"\n=== EXIT REASONS (OOS v7) ===", flush=True)
    for er, st in sorted(exit_stats.items(), key=lambda x: -x[1]['n']):
        wr = st['wins']/st['n']*100 if st['n'] > 0 else 0
        print(f"  {er:<20}  n={st['n']:>3}  WR={wr:>5.1f}%  PnL=${st['pnl']:+.3f}", flush=True)

# Save
out = '/tmp/clones_backtest/v7_oos_20260606.json'
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, 'w') as f:
    json.dump({'periods': [(n, str(s), str(e)) for n, s, e in PERIODS], 'all_pnl': all_pnl, 'max_dd': max_dd_period, 'n_trades': len(all_trades)}, f, indent=2, default=str)
print(f"\n✓ Saved to {out}", flush=True)
