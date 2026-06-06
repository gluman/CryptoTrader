#!/usr/bin/env python3
"""OOS validation v6: бэктест на разных sub-periods для проверки устойчивости."""
import os, sys, json
from datetime import datetime, timezone
sys.path.insert(0, '/home/andy/CryptoTrader')
from cryptotrader_strategies.clone_5_v6 import Clone5V6Strategy
from cryptotrader_strategies.run_backtest import run_backtest_for_strategy, compute_metrics

SYMBOLS = ["TONUSDT", "DOGEUSDT", "SUIUSDT"]
SIZE = 5.0
NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)

# 4 периода: 4 недели каждый (out-of-sample cross-validation)
PERIODS = [
    ("W1: 6.05-13.05", datetime(2026, 5, 6, tzinfo=timezone.utc), datetime(2026, 5, 13, tzinfo=timezone.utc)),
    ("W2: 13.05-20.05", datetime(2026, 5, 13, tzinfo=timezone.utc), datetime(2026, 5, 20, tzinfo=timezone.utc)),
    ("W3: 20.05-27.05", datetime(2026, 5, 20, tzinfo=timezone.utc), datetime(2026, 5, 27, tzinfo=timezone.utc)),
    ("W4: 27.05-3.06", datetime(2026, 5, 27, tzinfo=timezone.utc), datetime(2026, 6, 3, tzinfo=timezone.utc)),
    ("W5: 3.06-6.06", datetime(2026, 6, 3, tzinfo=timezone.utc), datetime(2026, 6, 6, 18, tzinfo=timezone.utc)),
]

print(f"v6 OOS Cross-Validation (4 weeks, 5 sub-periods)", flush=True)
print("="*100, flush=True)
print(f"{'Period':<20} {'Tr':>4} {'WR%':>5} {'PF':>5} {'PnL$':>6}", flush=True)
print("-"*50, flush=True)

all_trades = []
for name, start, end in PERIODS:
    c = Clone5V6Strategy()
    c.params.symbols = SYMBOLS
    try:
        trades = run_backtest_for_strategy(c, start, end, size_usdt=SIZE)
        m = compute_metrics(trades)
        all_trades.extend(trades)
        print(f"{name:<20} {m['trades']:>4} {m['wr_pct']:>5.1f} {m['pf']:>5.2f} ${m['pnl_usd']:>+5.2f}", flush=True)
    except Exception as e:
        print(f"{name:<20} ERROR: {e}", flush=True)

if all_trades:
    agg = compute_metrics(all_trades)
    print(f"\n  AGG (all weeks): tr={agg['trades']} WR={agg['wr_pct']:.1f}% PF={agg['pf']:.2f} PnL=${agg['pnl_usd']:+.2f} MaxDD=${agg['max_dd_usd']:+.2f} Sh={agg['sharpe_like']:.2f}", flush=True)

# Сохраняем
out = '/tmp/clones_backtest/v6_oos_20260606.json'
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, 'w') as f:
    json.dump({'oos_aggregated': agg if all_trades else None, 'periods': PERIODS}, f, indent=2, default=str)
print(f"\n✓ Saved to {out}", flush=True)
