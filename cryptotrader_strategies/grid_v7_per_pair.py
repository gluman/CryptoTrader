#!/usr/bin/env python3
"""
Per-pair grid для Clone5 v7: подобрать swing_lookback и sweep_threshold для 4 новых пар.
NEARUSDT, SOLUSDT, LITUSDT, WLDUSDT — default v7 params не оптимальны.

Grid: 4 swing_lookback × 3 sweep_threshold × 2 wick_body = 24 комбинаций / pair.
"""
import os, sys, json, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/andy/CryptoTrader')
from cryptotrader_strategies.clone_5_v7 import Clone5V7Strategy
from cryptotrader_strategies.clone_5_v7 import run_v7_backtest

# 4 новые пары (топ по FULL v7 backtest без per-pair tuning)
SYMBOLS = ["NEARUSDT", "SOLUSDT", "LITUSDT", "WLDUSDT"]

NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)
START = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)

# 24 комбинации (4×3×2)
GRID = {
    'swing_lookback': [30, 50, 80, 120],
    'sweep_threshold': [0.003, 0.005, 0.008],
    'wick_body_min_ratio': [0.8, 1.0],
}

print(f"=== v7 per-pair grid (4 pairs × 24 combos = 96 runs) ===", flush=True)
print(f"Period: {START.date()} → {NOW.date()} | Size: $1/pos", flush=True)
print("="*120, flush=True)

all_results = {}
for sym in SYMBOLS:
    print(f"\n>>> {sym} <<<", flush=True)
    best_pf = 0
    best_pnl = -100
    best_params = None
    best_metrics = None
    sym_results = []
    for sl in GRID['swing_lookback']:
        for sw in GRID['sweep_threshold']:
            for wb in GRID['wick_body_min_ratio']:
                c = Clone5V7Strategy()
                c.swing_lookback = sl
                c.sweep_threshold = sw
                c.wick_body_min_ratio = wb
                c.params.symbols = [sym]
                t0 = time.time()
                try:
                    r = run_v7_backtest(c, START, NOW, size_usdt=1.0, symbols=[sym])
                    m = r.get(sym) or {}
                    n = m.get('n', 0)
                    pf = m.get('pf', 0)
                    pnl = m.get('pnl_usd', 0)
                    wr = m.get('wr_pct', 0)
                    sh = pf  # use pf as quality proxy
                    dt = time.time() - t0
                    sym_results.append({
                        'sl': sl, 'sw': sw, 'wb': wb,
                        'trades': n, 'wr_pct': wr, 'pf': pf, 'pnl_usd': pnl
                    })
                    tag = 'PF+' if pf > 1.0 and n >= 3 else ('no_tr' if n == 0 else '')
                    print(f"  sl={sl:3d} sw={sw:.3f} wb={wb:.1f} | tr={n:>3} WR={wr:>5.1f}% PF={pf:>5.2f} PnL=${pnl:>+5.2f} ({dt:.1f}s) {tag}", flush=True)
                    if n >= 3 and pf > best_pf:
                        best_pf = pf
                        best_pnl = pnl
                        best_params = (sl, sw, wb)
                        best_metrics = m
                except Exception as e:
                    print(f"  sl={sl:3d} sw={sw:.3f} wb={wb:.1f} | ERROR: {e}", flush=True)
    all_results[sym] = {'best_params': best_params, 'best_metrics': best_metrics, 'all': sym_results}
    if best_params:
        sl, sw, wb = best_params
        m = best_metrics
        print(f"  >>> BEST for {sym}: sl={sl} sw={sw} wb={wb} | tr={m['n']} WR={m['wr_pct']:.1f}% PF={m['pf']:.2f} PnL=${m['pnl_usd']:+.2f}", flush=True)
    else:
        print(f"  >>> NO profitable params for {sym}", flush=True)

# Save
out_path = Path('/tmp/grid_v7_per_pair.json')
with open(out_path, 'w') as f:
    json.dump({
        'symbols': SYMBOLS,
        'period': f"{START.isoformat()} → {NOW.isoformat()}",
        'grid': GRID,
        'results': {sym: {
            'best_params': r['best_params'],
            'best_pf': r['best_metrics']['pf'] if r['best_metrics'] else 0,
            'best_pnl': r['best_metrics']['pnl_usd'] if r['best_metrics'] else 0,
        } for sym, r in all_results.items()}
    }, f, indent=2, default=str)

print(f"\n=== SAVED to {out_path} ===", flush=True)
print(f"\n=== SUMMARY ===", flush=True)
for sym, r in all_results.items():
    if r['best_params']:
        sl, sw, wb = r['best_params']
        m = r['best_metrics']
        print(f"  {sym}: BEST sl={sl} sw={sw} wb={wb} → PF={m['pf']:.2f} PnL=${m['pnl_usd']:+.2f}", flush=True)
    else:
        print(f"  {sym}: NO profitable params (default v7 only)", flush=True)
