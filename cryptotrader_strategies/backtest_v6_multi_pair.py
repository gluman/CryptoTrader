#!/usr/bin/env python3
"""v6 multi-pair backtest: проверяем стратегию на всех доступных парах с историей >= 14 дней."""
import os, sys, json
from datetime import datetime, timezone
sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')
import psycopg2
from cryptotrader_strategies.clone_5_v6 import Clone5V6Strategy
from cryptotrader_strategies.run_backtest import run_backtest_for_strategy, compute_metrics

# Получаем все пары с >= 14 днями 5m истории
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
        all_pairs.append((sym, bars, span))
cur.close()
conn.close()

print(f"Total pairs with >=14d history: {len(all_pairs)}", flush=True)
print("="*100, flush=True)

# Use 3-month period (same as before)
NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)
START_3M = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
SIZE = 1.0  # per-pair (so $1/pos, results easily comparable)

print(f"{'Symbol':<12} {'Bars':>5} {'Span':>5} {'Tr':>4} {'WR%':>5} {'PF':>5} {'PnL$':>7} {'Sharpe':>7}", flush=True)
print("-"*70, flush=True)

results = {}
all_trades = []
for sym, bars, span in all_pairs:
    c = Clone5V6Strategy()
    c.params.symbols = [sym]
    try:
        trades = run_backtest_for_strategy(c, START_3M, NOW, size_usdt=SIZE)
        m = compute_metrics(trades)
        results[sym] = m
        all_trades.extend(trades)
        print(f"{sym:<12} {bars:>5} {span:>3}d  {m['trades']:>4} {m['wr_pct']:>5.1f} {m['pf']:>5.2f} ${m['pnl_usd']:>+5.2f} {m['sharpe_like']:>7.2f}", flush=True)
    except Exception as e:
        print(f"{sym:<12} ERR: {str(e)[:60]}", flush=True)

# Агрегат по всем парам
if all_trades:
    agg = compute_metrics(all_trades)
    print("-"*70, flush=True)
    print(f"{'ALL (sum)':<12} {'':>5} {'':>5}  {agg['trades']:>4} {agg['wr_pct']:>5.1f} {agg['pf']:>5.2f} ${agg['pnl_usd']:>+5.2f} {agg['sharpe_like']:>7.2f}", flush=True)

# Сортируем по PF
profitable = sorted(
    [(s, r) for s, r in results.items() if r['trades'] >= 3 and r['pf'] > 0],
    key=lambda x: -x[1]['pf']
)
print(f"\n=== TOP 10 PAIRS BY PF (>= 3 trades) ===", flush=True)
for sym, m in profitable[:10]:
    print(f"  {sym:<12}  tr={m['trades']:>3}  PF={m['pf']:>5.2f}  PnL=${m['pnl_usd']:>+5.2f}", flush=True)

# Сохраняем
out = '/tmp/clones_backtest/v6_multi_pair_20260606.json'
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, 'w') as f:
    json.dump({s: m for s, m in results.items()}, f, indent=2, default=str)
print(f"\n✓ Saved to {out}", flush=True)
