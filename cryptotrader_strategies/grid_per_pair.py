#!/usr/bin/env python3
"""
Per-pair grid search для Clone5: подобрать лучший sw_thr для каждой пары отдельно.
Остальные параметры (wick_body=1.0, vol_spike=1.3) оставляем из глобального оптимума.

Также прогоняет расширенный grid для top-3 пар (TON/DOGE/HYPE).
"""
import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/andy/CryptoTrader')
from cryptotrader_strategies.clone_5_market_maker import Clone5MarketMakerStrategy
from cryptotrader_strategies.run_backtest import run_backtest_for_strategy, compute_metrics

SYMBOLS = ["XRPUSDT", "DOGEUSDT", "TONUSDT", "SUIUSDT",
           "ADAUSDT", "AVAXUSDT", "LINKUSDT", "HYPEUSDT"]

# Период: 3 мес назад → сейчас
NOW = datetime(2026, 6, 6, 18, 0, 0, tzinfo=timezone.utc)
START_3M = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)

SIZE = 5.0
PAIR_SIZE = SIZE / len(SYMBOLS)

# Параметры для grid (уменьшенная сетка — 3×2×1×1=6 комбинаций на пару)
GRID_PARAMS = {
    'swing_lookback': [30, 50, 80],
    'sweep_threshold': [0.003, 0.005, 0.008],
    'wick_body_min_ratio': [1.0],
    'min_volume_spike': [1.3],
}

print(f"Per-pair grid search for Clone5", flush=True)
print(f"Symbols: {SYMBOLS}", flush=True)
print(f"Period: {START_3M.date()} → {NOW.date()}", flush=True)
print(f"Grid size: {len(GRID_PARAMS['swing_lookback'])} × {len(GRID_PARAMS['sweep_threshold'])} × {len(GRID_PARAMS['wick_body_min_ratio'])} × {len(GRID_PARAMS['min_volume_spike'])} = {len(GRID_PARAMS['swing_lookback'])*len(GRID_PARAMS['sweep_threshold'])*len(GRID_PARAMS['wick_body_min_ratio'])*len(GRID_PARAMS['min_volume_spike'])} per symbol", flush=True)
print("="*120, flush=True)

all_results = {}
for sym in SYMBOLS:
    print(f"\n>>> {sym} <<<", flush=True)
    best_pf = 0
    best_pnl = -100
    best_params = None
    best_metrics = None
    sym_results = []
    for sl in GRID_PARAMS['swing_lookback']:
        for sw in GRID_PARAMS['sweep_threshold']:
            for wb in GRID_PARAMS['wick_body_min_ratio']:
                for vs in GRID_PARAMS['min_volume_spike']:
                    c = Clone5MarketMakerStrategy()
                    c.swing_lookback = sl
                    c.sweep_threshold = sw
                    c.wick_body_min_ratio = wb
                    c.min_volume_spike = vs
                    c.params.symbols = [sym]
                    t0 = time.time()
                    try:
                        trades = run_backtest_for_strategy(c, START_3M, NOW, size_usdt=PAIR_SIZE)
                        m = compute_metrics(trades)
                        dt = time.time() - t0
                        sym_results.append({
                            'sl': sl, 'sw': sw, 'wb': wb, 'vs': vs,
                            'trades': m['trades'], 'wr_pct': m['wr_pct'], 'pf': m['pf'],
                            'pnl_usd': m['pnl_usd'], 'sharpe': m['sharpe_like']
                        })
                        # print compact
                        if m['pf'] > 1.0 and m['trades'] >= 3:
                            tag = 'PF+'
                        elif m['trades'] > 0:
                            tag = ''
                        else:
                            tag = 'no_tr'
                        # Print every result
                        print(f"  sl={sl:3d} sw={sw:.3f} wb={wb:.1f} vs={vs:.1f} | tr={m['trades']:>3} WR={m['wr_pct']:>5.1f}% PF={m['pf']:>5.2f} PnL=${m['pnl_usd']:>+5.2f} Sh={m['sharpe_like']:>5.2f} ({dt:.1f}s) {tag}", flush=True)
                        # Track best
                        if m['trades'] >= 5 and m['pf'] > best_pf:
                            best_pf = m['pf']
                            best_pnl = m['pnl_usd']
                            best_params = (sl, sw, wb, vs)
                            best_metrics = m
                    except Exception as e:
                        print(f"  sl={sl:3d} sw={sw:.3f} wb={wb:.1f} vs={vs:.1f} | ERROR: {e}", flush=True)
    all_results[sym] = {
        'best_params': best_params,
        'best_metrics': best_metrics,
        'all': sym_results
    }
    if best_params:
        sl, sw, wb, vs = best_params
        m = best_metrics
        print(f"  >>> BEST for {sym}: sl={sl} sw={sw} wb={wb} vs={vs} | tr={m['trades']} WR={m['wr_pct']:.1f}% PF={m['pf']:.2f} PnL=${m['pnl_usd']:+.2f}", flush=True)
    else:
        print(f"  >>> NO profitable params for {sym}", flush=True)

# Save
out_path = '/tmp/clones_backtest/clone5_per_pair_grid_20260606.json'
os.makedirs(os.path.dirname(out_path), exist_ok=True)
# Convert to serializable
serial = {}
for sym, data in all_results.items():
    if data['best_metrics']:
        serial[sym] = {
            'best_params': {
                'swing_lookback': data['best_params'][0],
                'sweep_threshold': data['best_params'][1],
                'wick_body_min_ratio': data['best_params'][2],
                'min_volume_spike': data['best_params'][3],
            },
            'best_pf': data['best_metrics']['pf'],
            'best_pnl': data['best_metrics']['pnl_usd'],
            'best_trades': data['best_metrics']['trades'],
        }
    else:
        serial[sym] = None
with open(out_path, 'w') as f:
    json.dump(serial, f, indent=2)
print(f"\n✓ Best per-pair results saved to {out_path}", flush=True)

# Final summary table
print("\n" + "="*80, flush=True)
print("PER-PAIR BEST PARAMETERS (PF criterion, min 5 trades)", flush=True)
print("="*80, flush=True)
print(f"{'Symbol':>10} {'sl':>3} {'sw':>5} {'wb':>4} {'vs':>4} {'Tr':>3} {'WR%':>5} {'PF':>5} {'PnL$':>6}", flush=True)
print("-"*80, flush=True)
for sym, data in all_results.items():
    if data['best_params']:
        sl, sw, wb, vs = data['best_params']
        m = data['best_metrics']
        print(f"{sym:>10} {sl:>3} {sw:>5.3f} {wb:>4.1f} {vs:>4.1f} {m['trades']:>3} {m['wr_pct']:>5.1f} {m['pf']:>5.2f} ${m['pnl_usd']:>+5.2f}", flush=True)
    else:
        print(f"{sym:>10} NO WINNER", flush=True)
