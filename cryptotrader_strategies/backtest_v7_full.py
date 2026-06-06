#!/usr/bin/env python3
"""Полный v7 multi-pair backtest: ВСЕ доступные пары за доступную историю."""
import os, sys, json
from datetime import datetime, timezone
sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')
import psycopg2
from cryptotrader_strategies.clone_5_v7 import Clone5V7Strategy, run_v7_backtest

DB = dict(host="192.168.0.149", port=5432, database="cryptotrader", user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"])
conn = psycopg2.connect(**DB)
cur = conn.cursor()
cur.execute("""
    SELECT symbol, COUNT(*) as bars, MIN(timestamp) as oldest, MAX(timestamp) as newest
    FROM ohlcv_raw
    WHERE exchange='bybit' AND timeframe='5m'
    GROUP BY symbol
    HAVING COUNT(*) >= 4000
    ORDER BY bars DESC
""")
all_pairs = []
for r in cur.fetchall():
    sym, bars, oldest, newest = r
    span = (newest - oldest).days
    if span >= 14:
        all_pairs.append(sym)
cur.close()
conn.close()

NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)
START = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
SIZE = 1.0  # per pair $1

# Exclude BSBUSDT (per Boss's decision)
EXCLUDE = ["BSBUSDT"]
pairs_to_test = [p for p in all_pairs if p not in EXCLUDE]
print(f"=== v7 FULL Multi-Pair Backtest ===", flush=True)
print(f"Total pairs: {len(pairs_to_test)} (excluded: {EXCLUDE})", flush=True)
print(f"Period: {START.date()} → {NOW.date()} | Size: $1/pos", flush=True)
print("="*100, flush=True)

c = Clone5V7Strategy()
c.params.symbols = pairs_to_test
results = run_v7_backtest(c, START, NOW, size_usdt=SIZE, symbols=pairs_to_test)

print(f"\n{'Pair':<14} {'Tr':>4} {'WR%':>5} {'PF':>5} {'PnL$':>7} {'MaxDD$':>7} {'Step':>4} {'Blackout':>8}", flush=True)
print("-"*70, flush=True)

total_pnl = 0
all_trades = []
for sym, r in sorted(results.items(), key=lambda x: -x[1].get('pf', 0)):
    total_pnl += r['pnl_usd']
    all_trades.extend(r['trades'])
    pf = r['pf'] if r['pf'] != 10.0 or r['n'] > 0 else 0.0
    print(f"{sym:<14} {r['n']:>4} {r['wr_pct']:>5.1f} {pf:>5.2f} ${r['pnl_usd']:>+6.2f} ${r['max_dd_usd']:>5.2f} {r['final_step']:>4} {r['blackouts']:>8}", flush=True)

if all_trades:
    pnls = [t.pnl_dollar for t in all_trades]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p <= 0))
    pf = gross_w/gross_l if gross_l > 0 else 10.0
    print("-"*70, flush=True)
    print(f"{'TOTAL':<14} {len(pnls):>4} {wins/len(pnls)*100:>5.1f} {pf:>5.2f} ${sum(pnls):>+6.2f}", flush=True)

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
    print(f"\n=== EXIT REASONS (v7 FULL) ===", flush=True)
    for er, st in sorted(exit_stats.items(), key=lambda x: -x[1]['n']):
        wr = st['wins']/st['n']*100 if st['n'] > 0 else 0
        print(f"  {er:<20}  n={st['n']:>3}  WR={wr:>5.1f}%  PnL=${st['pnl']:+.3f}", flush=True)

# Profitable pairs count
profitable_pairs = [(s, r) for s, r in results.items() if r['n'] >= 3 and r['pf'] >= 1.0 and r['pf'] != 10.0]
print(f"\n=== SUMMARY ===", flush=True)
print(f"Profitable pairs (PF>=1, >=3 trades): {len(profitable_pairs)}/{len(results)}", flush=True)
print(f"Total PnL: ${sum(r['pnl_usd'] for r in results.values()):+.2f}", flush=True)

# Save
out = '/tmp/clones_backtest/v7_full_multi_20260606.json'
os.makedirs(os.path.dirname(out), exist_ok=True)
serial = {sym: {k: v for k, v in r.items() if k != 'trades'} for sym, r in results.items()}
with open(out, 'w') as f:
    json.dump(serial, f, indent=2, default=str)
print(f"\n✓ Saved to {out}", flush=True)
