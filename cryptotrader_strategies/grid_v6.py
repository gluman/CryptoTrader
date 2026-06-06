#!/usr/bin/env python3
"""Grid search для Clone5 v6: подбор VSA-параметров."""
import os, sys, time, json
from datetime import datetime, timezone
sys.path.insert(0, '/home/andy/CryptoTrader')
from cryptotrader_strategies.clone_5_v6 import Clone5V6Strategy
from cryptotrader_strategies.run_backtest import run_backtest_for_strategy, compute_metrics

SYMBOLS = ["TONUSDT", "DOGEUSDT", "SUIUSDT"]
NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)
START_3M = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
SIZE = 5.0
PAIR_SIZE = SIZE / len(SYMBOLS)

# Только 1 параметр: min_score (чтобы не убить v6 убирая бонусы)
GRID = {
    'min_score_to_trade': [0.50, 0.55, 0.60, 0.65, 0.70],
    'vsa_effort_threshold': [1.2, 1.5, 2.0],
    'vsa_climax_multiplier': [2.0, 3.0, 4.0],
}

print(f"v6 VSA grid: {len(GRID['min_score_to_trade'])}×{len(GRID['vsa_effort_threshold'])}×{len(GRID['vsa_climax_multiplier'])} = {len(GRID['min_score_to_trade'])*len(GRID['vsa_effort_threshold'])*len(GRID['vsa_climax_multiplier'])} комбинаций", flush=True)
print("="*100, flush=True)

best_pf = 0
best_combo = None
results = []
for ms in GRID['min_score_to_trade']:
    for ve in GRID['vsa_effort_threshold']:
        for vc in GRID['vsa_climax_multiplier']:
            c = Clone5V6Strategy()
            c.min_score_to_trade = ms
            c.vsa_effort_threshold = ve
            c.vsa_climax_multiplier = vc
            c.params.symbols = SYMBOLS
            t0 = time.time()
            try:
                trades = run_backtest_for_strategy(c, START_3M, NOW, size_usdt=SIZE)
                m = compute_metrics(trades)
                dt = time.time() - t0
                results.append({'ms': ms, 've': ve, 'vc': vc, 'trades': m['trades'], 'wr': m['wr_pct'], 'pf': m['pf'], 'pnl': m['pnl_usd']})
                tag = ''
                if m['pf'] > best_pf and m['trades'] >= 5:
                    best_pf = m['pf']
                    best_combo = (ms, ve, vc)
                if m['trades'] > 0:
                    print(f"  ms={ms:.2f} ve={ve:.1f} vc={vc:.1f} | tr={m['trades']:>3} WR={m['wr_pct']:>5.1f}% PF={m['pf']:>5.2f} PnL=${m['pnl_usd']:>+5.2f} ({dt:.1f}s)", flush=True)
            except Exception as e:
                print(f"  ms={ms:.2f} ve={ve:.1f} vc={vc:.1f} | ERROR: {e}", flush=True)

if best_combo:
    print(f"\n=== BEST: ms={best_combo[0]:.2f} ve={best_combo[1]:.1f} vc={best_combo[2]:.1f} → PF={best_pf:.2f}", flush=True)

# Top 5
results.sort(key=lambda x: -x['pf'])
print(f"\n=== TOP 5 by PF ===", flush=True)
for r in results[:5]:
    print(f"  ms={r['ms']:.2f} ve={r['ve']:.1f} vc={r['vc']:.1f} | tr={r['trades']:>3} PF={r['pf']:>5.2f} PnL=${r['pnl']:>+5.2f}", flush=True)
