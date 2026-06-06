#!/usr/bin/env python3
"""
Grid search optimizer для 4 клонов. Ищет параметры, при которых:
  - back.PF > 1.0
  - forward.PF > 1.0
  - forward.PnL > 0

Перебирает: min_confidence, sl_pct, tp_pct, trailing_step, score_thresholds, time_decision_every_n_bars.

Использование:
    python cryptotrader_strategies/optimizer.py --clone clone0_current
    python cryptotrader_strategies/optimizer.py --clone clone1_low_risk --max-combos 200
    python cryptotrader_strategies/optimizer.py --clone all
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cryptotrader_strategies import get_all_strategies
from cryptotrader_strategies.run_backtest import (
    run_backtest_for_strategy, compute_metrics, _empty_metrics
)

# Периоды — те же что в run_backtest
now = datetime.now(timezone.utc)
BACK_START = now - __import__('pandas').Timedelta(days=30)
BACK_END = now - __import__('pandas').Timedelta(days=16)
FWD_START = now - __import__('pandas').Timedelta(days=14)
FWD_END = now


# === Grid spaces per clone ===

GRID_CLONE0 = {
    "min_confidence": [0.40, 0.50, 0.60, 0.70, 0.80],
    "sl_pct":         [0.5, 0.7, 1.0, 1.5, 2.0],
    "tp_pct":         [0.7, 1.0, 1.5, 2.0, 3.0],
    "trailing_step_pct": [0.0, 0.1, 0.2, 0.3],
    "decision_every_n_bars": [1, 3, 6, 12],
}

GRID_CLONE1 = {
    "min_confidence": [0.30, 0.40, 0.50, 0.60, 0.70],
    "sl_pct":         [0.7, 1.0, 1.5, 2.0, 2.5, 3.0],
    "tp_pct":         [1.0, 1.5, 2.0, 2.5, 3.0, 4.0],
    "trailing_step_pct": [0.0, 0.1, 0.2, 0.3, 0.5],
    "decision_every_n_bars": [1, 2, 4],
}

GRID_CLONE2_3B = {
    "min_confidence": [0.30, 0.40, 0.50, 0.60],
    "contrarian_invert_threshold": [0.4, 0.5, 0.6],
    "sl_pct":         [0.5, 0.7, 1.0, 1.5],
    "tp_pct":         [0.7, 1.0, 1.5, 2.0],
    "trailing_step_pct": [0.0, 0.1, 0.2],
    "decision_every_n_bars": [1, 3],
}

GRID_CLONE2_3C = {
    "min_confidence": [0.40, 0.50, 0.60, 0.70],
    "sentiment_bullish_threshold": [0.6, 0.7, 0.8],
    "sl_pct":         [0.5, 0.7, 1.0, 1.5],
    "tp_pct":         [0.7, 1.0, 1.5, 2.0],
    "trailing_step_pct": [0.0, 0.1, 0.2],
    "decision_every_n_bars": [1, 3],
}


def expand_grid(grid: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    """Декартово произведение всех значений в grid."""
    keys = list(grid.keys())
    values = list(grid.values())
    combos = []
    for v in itertools.product(*values):
        combos.append(dict(zip(keys, v)))
    return combos


def apply_params(strat, params: Dict[str, Any]) -> None:
    """Подменить поля в params + применить decision_every_n_bars через monkey-patch."""
    for k, v in params.items():
        if k == "decision_every_n_bars":
            continue
        if hasattr(strat.params, k):
            setattr(strat.params, k, v)
    # decision_every_n_bars хранится в run_backtest_for_strategy — сохраняем в strat
    strat._decision_every_n = params.get("decision_every_n_bars", 1)


def run_with_decision_every(strat, start, end, size_usdt):
    """Wrapper вокруг run_backtest_for_strategy с подменой decision_every_n."""
    n = getattr(strat, "_decision_every_n", 1)
    # Используем существующую функцию, но с подменой через monkey-patch:
    import cryptotrader_strategies.run_backtest as rt
    orig = rt.run_backtest_for_strategy

    def patched(strat, start, end, size_usdt, decision_every_n_bars=1, max_bars_per_trade=None):
        return orig(strat, start, end, size_usdt, decision_every_n_bars=n, max_bars_per_trade=max_bars_per_trade)

    rt.run_backtest_for_strategy = patched
    try:
        return orig(strat, start, end, size_usdt, decision_every_n_bars=n)
    finally:
        rt.run_backtest_for_strategy = orig


def evaluate_combo(strat, params: Dict[str, Any], size_usdt: float = 5.0) -> Dict[str, Any]:
    """Запустить одну комбинацию на back+forward, вернуть метрики."""
    apply_params(strat, params)

    t0 = time.time()
    back_trades = run_with_decision_every(strat, BACK_START, BACK_END, size_usdt)
    back_m = compute_metrics(back_trades)
    fwd_trades = run_with_decision_every(strat, FWD_START, FWD_END, size_usdt)
    fwd_m = compute_metrics(fwd_trades)
    elapsed = time.time() - t0

    return {
        "params": params,
        "back": back_m,
        "fwd": fwd_m,
        "elapsed_s": round(elapsed, 1),
        # Score = учитываем ОБА периода, штрафуем если один плохой
        "score": _score(back_m, fwd_m),
    }


def _score(back: Dict, fwd: Dict) -> float:
    """Комплексный score: PF на обоих периодах, штраф за drawdown, бонус за PnL."""
    if fwd["trades"] < 5 or back["trades"] < 5:
        return -1e9  # слишком мало трейдов — не статистически значимо
    pf_min = min(back["pf"], fwd["pf"])
    pnl_sum = back["pnl_usd"] + fwd["pnl_usd"]
    dd_penalty = -max(0, fwd["max_dd_usd"]) * 0.5
    wr_bonus = (fwd["wr_pct"] - 50) * 0.1 if fwd["pf"] > 1.0 else 0
    # Главное — оба периода в плюсе
    if back["pnl_usd"] > 0 and fwd["pnl_usd"] > 0:
        base = 100 + pnl_sum * 10 + pf_min * 20 + wr_bonus
    else:
        base = pf_min * 5 + pnl_sum * 2 + dd_penalty
    return base


def grid_search(strat_name: str, max_combos: int = 100, top_n: int = 10) -> List[Dict]:
    """Прогнать grid search для одного клона."""
    all_strats = get_all_strategies()
    if strat_name not in all_strats:
        raise ValueError(f"Unknown strategy: {strat_name}. Available: {list(all_strats.keys())}")

    if strat_name == "clone0_current":
        grid = GRID_CLONE0
    elif strat_name == "clone1_low_risk":
        grid = GRID_CLONE1
    elif strat_name == "clone2_contrarian_3b":
        grid = GRID_CLONE2_3B
    elif strat_name == "clone2_contrarian_3c":
        grid = GRID_CLONE2_3C
    else:
        raise ValueError(f"No grid defined for {strat_name}")

    combos = expand_grid(grid)
    if len(combos) > max_combos:
        np.random.seed(42)
        idx = np.random.choice(len(combos), size=max_combos, replace=False)
        combos = [combos[i] for i in sorted(idx)]

    print(f"\n{'='*80}")
    print(f"  GRID SEARCH: {strat_name}")
    print(f"  Combos: {len(combos)}  |  Grid keys: {list(grid.keys())}")
    print(f"{'='*80}")

    results = []
    t_start = time.time()
    for i, combo in enumerate(combos):
        if i % 5 == 0:
            elapsed = time.time() - t_start
            eta = elapsed / max(i, 1) * (len(combos) - i)
            print(f"  [{i+1}/{len(combos)}] elapsed={elapsed:.0f}s ETA={eta:.0f}s")
        strat = all_strats[strat_name]  # fresh instance (но params общий)
        # fresh instance — в get_all_strategies каждый раз новый
        try:
            r = evaluate_combo(strat, combo, size_usdt=5.0)
            results.append(r)
        except Exception as e:
            print(f"  ERROR on combo {combo}: {e}")
            continue

    # Sort by score
    results.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n  TOP {min(top_n, len(results))} результатов:")
    print(f"  {'Score':>8} {'B.Tr':>5} {'B.WR%':>6} {'B.PnL':>7} {'B.PF':>5} {'F.Tr':>5} {'F.WR%':>6} {'F.PnL':>7} {'F.PF':>5}  Params")
    for r in results[:top_n]:
        b, f = r["back"], r["fwd"]
        params_short = ", ".join(f"{k.split('_')[0][:3]}={v}" for k, v in r["params"].items())
        print(f"  {r['score']:>8.1f} {b['trades']:>5} {b['wr_pct']:>6} {b['pnl_usd']:>7} {b['pf']:>5} "
              f"{f['trades']:>5} {f['wr_pct']:>6} {f['pnl_usd']:>7} {f['pf']:>5}  {params_short}")

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clone", default="all", help="Имя клона или 'all'")
    parser.add_argument("--max-combos", type=int, default=80)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--out", default="/tmp/clones_backtest/optimization.json")
    args = parser.parse_args()

    if args.clone == "all":
        names = ["clone0_current", "clone1_low_risk", "clone2_contrarian_3b", "clone2_contrarian_3c"]
    else:
        names = [args.clone]

    all_results = {}
    for name in names:
        results = grid_search(name, max_combos=args.max_combos, top_n=args.top)
        all_results[name] = results

    with open(args.out, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n\nResults saved: {args.out}")


if __name__ == "__main__":
    main()
