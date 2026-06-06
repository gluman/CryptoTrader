#!/usr/bin/env python3
"""Анализ v6 trades: consecutive losses, average streaks, martingale sizing."""
import os, sys, json
from datetime import datetime, timezone
sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')
import psycopg2
import numpy as np
from collections import Counter
from cryptotrader_strategies.clone_5_v6 import Clone5V6Strategy
from cryptotrader_strategies.run_backtest import run_backtest_for_strategy

DB = dict(host="192.168.0.149", port=5432, database="cryptotrader", user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"])

# 3-month period
NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)
START_3M = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
SIZE = 1.0

# Используем только ТОП-3 пары (которые в проде) + NEAR/SOL/BSB/ZEC (новые кандидаты)
PAIRS_TO_ANALYZE = ["TONUSDT", "DOGEUSDT", "SUIUSDT", "NEARUSDT", "SOLUSDT", "BSBUSDT", "ZECUSDT", "ONDOUSDT", "LITUSDT"]

all_trades_per_pair = {}
all_trades_combined = []

for sym in PAIRS_TO_ANALYZE:
    c = Clone5V6Strategy()
    c.params.symbols = [sym]
    try:
        trades = run_backtest_for_strategy(c, START_3M, NOW, size_usdt=SIZE)
        all_trades_per_pair[sym] = trades
        all_trades_combined.extend(trades)
    except Exception as e:
        print(f"{sym}: ERR {e}", flush=True)

print(f"Total trades collected: {len(all_trades_combined)}", flush=True)
print("="*100, flush=True)

# Сохраняем все trades
out = '/tmp/clones_backtest/v6_trades_per_pair.json'
with open(out, 'w') as f:
    json.dump({sym: [{'pnl_pct_net': float(t.pnl_pct_net), 'pnl_dollar': float(t.pnl_dollar), 'side': t.side, 'exit_reason': t.exit_reason, 'entry_time': str(t.entry_time), 'exit_time': str(t.exit_time)} for t in trades] for sym, trades in all_trades_per_pair.items()}, f, indent=2, default=str)
print(f"✓ Saved trades to {out}", flush=True)

# === АНАЛИЗ 1: Consecutive losses per pair ===
print(f"\n=== CONSECUTIVE LOSSES (per pair + combined) ===", flush=True)
print(f"{'Pair':<14} {'Tr':>4} {'W':>4} {'L':>4} {'WR%':>5} {'MaxLossStrk':>10} {'AvgLossStrk':>10} {'MaxWinStrk':>10} {'AvgWinStrk':>10}", flush=True)
print("-"*80, flush=True)

def analyze_streaks(pnl_list):
    """Return max/avg consecutive losses and wins."""
    if not pnl_list:
        return 0, 0, 0, 0
    max_l = 0
    cur_l = 0
    max_w = 0
    cur_w = 0
    all_l_streaks = []
    all_w_streaks = []
    for p in pnl_list:
        if p < 0:
            cur_l += 1
            cur_w = 0
            if cur_l > max_l:
                max_l = cur_l
        else:
            if cur_l > 0:
                all_l_streaks.append(cur_l)
            cur_l = 0
            cur_w += 1
            if cur_w > max_w:
                max_w = cur_w
        if p > 0:
            if cur_w > 0:
                all_w_streaks.append(cur_w)
    if cur_l > 0:
        all_l_streaks.append(cur_l)
    if cur_w > 0:
        all_w_streaks.append(cur_w)
    avg_l = np.mean(all_l_streaks) if all_l_streaks else 0
    avg_w = np.mean(all_w_streaks) if all_w_streaks else 0
    return max_l, avg_l, max_w, avg_w

# Per pair
for sym, trades in all_trades_per_pair.items():
    if not trades:
        continue
    pnls = [t.pnl_pct_net for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    wr = wins / len(pnls) * 100 if pnls else 0
    max_l, avg_l, max_w, avg_w = analyze_streaks(pnls)
    print(f"{sym:<14} {len(trades):>4} {wins:>4} {losses:>4} {wr:>5.1f} {max_l:>10} {avg_l:>10.1f} {max_w:>10} {avg_w:>10.1f}", flush=True)

# Combined
all_pnls = [t.pnl_pct_net for t in all_trades_combined]
wins = sum(1 for p in all_pnls if p > 0)
losses = sum(1 for p in all_pnls if p <= 0)
wr = wins / len(all_pnls) * 100 if all_pnls else 0
max_l, avg_l, max_w, avg_w = analyze_streaks(all_pnls)
print(f"{'COMBINED':<14} {len(all_trades_combined):>4} {wins:>4} {losses:>4} {wr:>5.1f} {max_l:>10} {avg_l:>10.1f} {max_w:>10} {avg_w:>10.1f}", flush=True)

# === АНАЛИЗ 2: Distribution of consecutive losses ===
print(f"\n=== CONSECUTIVE LOSS DISTRIBUTION (combined) ===", flush=True)
all_l_streaks = []
cur = 0
for p in all_pnls:
    if p <= 0:
        cur += 1
    else:
        if cur > 0:
            all_l_streaks.append(cur)
        cur = 0
if cur > 0:
    all_l_streaks.append(cur)

streak_counter = Counter(all_l_streaks)
total_streaks = len(all_l_streaks)
print(f"Total loss streaks: {total_streaks}")
for n in sorted(streak_counter.keys()):
    count = streak_counter[n]
    pct = count / total_streaks * 100
    cum_pct = sum(streak_counter[k] for k in streak_counter if k >= n) / total_streaks * 100
    print(f"  {n} consecutive losses: {count:>3} streaks ({pct:>5.1f}%) — cum>=n: {cum_pct:.1f}%")

# === АНАЛИЗ 3: Сумма убытков per streak ===
print(f"\n=== AVG $ LOSS PER STREAK ===", flush=True)
streak_pnls = {n: [] for n in range(1, max(streak_counter.keys())+1)}
i = 0
in_streak = 0
streak_pnl_sum = 0.0
streak_pnl_list = []
for t in all_trades_combined:
    pnl = t.pnl_dollar
    if pnl <= 0:
        in_streak += 1
        streak_pnl_sum += pnl
        streak_pnl_list.append(t)
    else:
        if in_streak > 0:
            streak_pnls[in_streak].append(streak_pnl_sum)
            in_streak = 0
            streak_pnl_sum = 0.0
            streak_pnl_list = []
if in_streak > 0:
    streak_pnls[in_streak].append(streak_pnl_sum)

for n in sorted(streak_pnls.keys()):
    losses = streak_pnls[n]
    if losses:
        avg = np.mean(losses)
        worst = min(losses)
        best = max(losses)
        print(f"  {n}-step streak: avg=${avg:.3f}  worst=${worst:.3f}  best=${best:.3f}  (n={len(losses)})", flush=True)

# === АНАЛИЗ 4: Анализ по exit_reason ===
print(f"\n=== EXIT REASON ANALYSIS (combined) ===", flush=True)
exit_stats = {}
for t in all_trades_combined:
    er = t.exit_reason
    if er not in exit_stats:
        exit_stats[er] = {'count': 0, 'pnl': 0.0, 'wins': 0}
    exit_stats[er]['count'] += 1
    exit_stats[er]['pnl'] += t.pnl_dollar
    if t.pnl_dollar > 0:
        exit_stats[er]['wins'] += 1
for er, st in sorted(exit_stats.items(), key=lambda x: -x[1]['count']):
    wr = st['wins'] / st['count'] * 100 if st['count'] > 0 else 0
    print(f"  {er:<20}  n={st['count']:>3}  WR={wr:>5.1f}%  PnL=${st['pnl']:+.3f}", flush=True)
