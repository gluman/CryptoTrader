#!/usr/bin/env python3
"""
Полный 3-мес backtest Clone5 на 8 парах + sub-period анализ.
"""
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/andy/CryptoTrader')
from cryptotrader_strategies.clone_5_market_maker import Clone5MarketMakerStrategy
from cryptotrader_strategies.run_backtest import run_backtest_for_strategy, compute_metrics

# Пары Clone5
SYMBOLS = ["XRPUSDT", "DOGEUSDT", "TONUSDT", "SUIUSDT",
           "ADAUSDT", "AVAXUSDT", "LINKUSDT", "HYPEUSDT"]

# Период: 3 мес назад → сейчас
NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)
START_3M = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)  # 91 дн назад

# Sub-периоды для cross-val
M1_START = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
M1_END   = datetime(2026, 4, 7, 0, 0, 0, tzinfo=timezone.utc)
M2_START = datetime(2026, 4, 7, 0, 0, 0, tzinfo=timezone.utc)
M2_END   = datetime(2026, 5, 7, 0, 0, 0, tzinfo=timezone.utc)
M3_START = datetime(2026, 5, 7, 0, 0, 0, tzinfo=timezone.utc)
M3_END   = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)

PERIODS = [
    ("FULL_3M",  START_3M, NOW),
    ("MAR_M1",   M1_START, M1_END),
    ("APR_M2",   M2_START, M2_END),
    ("MAY_M3",   M3_START, M3_END),
]

SIZE = 5.0
PAIR_SIZE = SIZE / len(SYMBOLS)  # ~$0.625/pos на пару (с учётом комиссий в round-trip)

print(f"Clone5 (Liquidity Hunt) 3-month backtest")
print(f"Symbols: {SYMBOLS}")
print(f"Total size: ${SIZE}, per-symbol: ${PAIR_SIZE:.2f}")
print(f"Periods: {[p[0] for p in PERIODS]}")
print("="*120)

results = {}
for period_name, start, end in PERIODS:
    print(f"\n--- PERIOD: {period_name} ({start.date()} → {end.date()}) ---", flush=True)
    print(f"{'Symbol':>10} {'Trades':>7} {'WR%':>6} {'AvgW%':>7} {'AvgL%':>7} {'PF':>5} {'PnL$':>7} {'MaxDD%':>7} {'Sharpe':>7}", flush=True)
    print("-"*80, flush=True)
    period_trades = []
    period_pnl = 0.0
    for sym in SYMBOLS:
        c = Clone5MarketMakerStrategy()  # default = optimized (sw=0.003, wb=1.0, vol=1.3)
        c.params.symbols = [sym]  # только эта пара
        try:
            trades = run_backtest_for_strategy(c, start, end, size_usdt=PAIR_SIZE)
            m = compute_metrics(trades)
            results[(period_name, sym)] = m
            period_trades.extend(trades)
            period_pnl += m['pnl_usd']
            print(f"{sym:>10} {m['trades']:>7} {m['wr_pct']:>6.1f} {m['avg_win_pct']:>7.2f} {m['avg_loss_pct']:>7.2f} {m['pf']:>5.2f} ${m['pnl_usd']:>+5.2f} {m['max_dd_usd']:>+7.2f} {m['sharpe_like']:>7.2f}", flush=True)
        except Exception as e:
            print(f"{sym:>10} ERROR: {e}", flush=True)
            results[(period_name, sym)] = None
    # Period aggregate
    if period_trades:
        agg = compute_metrics(period_trades)
        print(f"\n  PERIOD AGGREGATE: trades={agg['trades']:>3} WR={agg['wr_pct']:.1f}% PF={agg['pf']:.2f} PnL=${agg['pnl_usd']:+.2f} MaxDD$={agg['max_dd_usd']:+.2f} Sharpe={agg['sharpe_like']:.2f}", flush=True)
        results[(period_name, '__AGG__')] = agg
    else:
        print(f"  PERIOD {period_name}: no trades", flush=True)

# Save full results
out_path = '/tmp/clones_backtest/clone5_3m_20260606.json'
os.makedirs(os.path.dirname(out_path), exist_ok=True)
# Convert tuple keys
serializable = {f"{p}|{s}": m for (p, s), m in results.items()}
with open(out_path, 'w') as f:
    json.dump(serializable, f, indent=2, default=str)
print(f"\n✓ Full results saved to {out_path}")
