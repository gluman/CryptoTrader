#!/usr/bin/env python3
"""
Clone5 v7 ABLATION — разделить trailing stop и martingale, прогнать на ВСЕХ парах.

Три варианта (v7 НЕ модифицируется):
  v7_full       = trailing ON + martingale ON       (текущий прод)
  v7a_trailing  = trailing ON + martingale OFF      (только trailing)
  v7b_martingale= trailing OFF + martingale ON      (только martingale)

Period: 3 месяца (или сколько доступно) на 5m TF.
Pairs:  все с >=4000 баров (≈28 пар).
Size:   $1/pos для честного cross-pair scaling.
"""
from __future__ import annotations

import os
import sys
import json
import time
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import psycopg2

from cryptotrader_strategies.clone_5_v7 import (
    Clone5V7Strategy,
    MartingaleState,
    MARTINGALE_LEVELS,
    MAX_CONSECUTIVE_LOSSES,
    COOLDOWN_HOURS,
    DAILY_LOSS_LIMIT,
    PER_PAIR_EXPOSURE_CAP,
    PER_PAIR_PARAMS_V7,
)
from cryptotrader_strategies.base_strategy import StrategyParams
from cryptotrader_strategies.run_backtest import (
    load_ohlcv, _simulate_trade, compute_metrics, TradeResult, TF_MAP,
)

DB = dict(
    host="192.168.0.149", port=5432, database="cryptotrader",
    user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"],
)

EXCLUDE = ["BSBUSDT"]  # Boss excluded earlier


def get_pairs(min_bars: int = 4000) -> List[str]:
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, COUNT(*) as bars, MIN(timestamp), MAX(timestamp)
        FROM ohlcv_raw
        WHERE exchange='bybit' AND timeframe='5m'
        GROUP BY symbol
        HAVING COUNT(*) >= %s
        ORDER BY bars DESC
    """, (min_bars,))
    out = []
    for r in cur.fetchall():
        sym, bars, oldest, newest = r
        span = (newest - oldest).days
        if span >= 14 and sym not in EXCLUDE:
            out.append(sym)
    cur.close()
    conn.close()
    return out


def run_ablation(strategy: Clone5V7Strategy, start, end, size_usdt: float,
                 symbols: List[str], *, use_martingale: bool,
                 use_trailing: bool) -> Dict[str, Dict[str, Any]]:
    """Per-pair backtest с заданной комбинацией trailing/martingale.

    use_martingale=False: переопределяем get_position_size → всегда базовый size.
    use_trailing=False:    params.trailing_enabled=False → симулятор не подтягивает SL.
    """
    tf_minutes = 5
    max_bars_per_trade = int(strategy.params.max_hold_minutes / tf_minutes)
    if not use_trailing:
        strategy.params.trailing_enabled = False

    results = {}
    for symbol in symbols:
        df = load_ohlcv(symbol, strategy.params.timeframe, start, end)
        if df.empty or len(df) < 60:
            continue

        state = strategy._get_state(symbol)
        # Сброс state (per-run)
        state.consecutive_losses = 0
        state.current_step = 0
        state.daily_pnl_today = 0.0
        state.daily_date = None
        state.blackout_until = None

        trades: List[TradeResult] = []
        i = 60
        while i < len(df) - 1:
            history = df.iloc[:i + 1]
            current_ts = history.index[-1]

            if state.is_blackout(current_ts):
                i += 1
                continue
            if state.daily_loss_exceeded():
                i += 1
                continue

            decision = strategy.decide(history, symbol)
            sig = decision.get("signal")
            conf = decision.get("confidence", 0.0)
            sl_pct = decision.get("stop_loss_pct", 0.0)
            tp_pct = decision.get("take_profit_pct", 0.0)
            side = decision.get("side")

            if sig in ("BUY", "SELL") and conf >= strategy.params.min_confidence and side and sl_pct > 0:
                # === Martingale size (или базовый) ===
                if use_martingale:
                    pos_size = state.get_position_size(size_usdt)
                    if pos_size > PER_PAIR_EXPOSURE_CAP:
                        pos_size = PER_PAIR_EXPOSURE_CAP
                else:
                    pos_size = size_usdt
                trade = _simulate_trade(
                    df=df, entry_idx=i, side=side, entry_price=float(df.iloc[i]["close"]),
                    entry_time=current_ts, sl_pct=sl_pct, tp_pct=tp_pct,
                    max_bars=max_bars_per_trade, tf_minutes=tf_minutes,
                    strategy=strategy, size_usdt=pos_size,
                )
                trades.append(trade)
                pnl = trade.pnl_dollar
                if pnl > 0:
                    state.record_win(trade.exit_time, pnl)
                else:
                    state.record_loss(trade.exit_time, pnl)
                i = trade.exit_idx + 1
            else:
                i += 1

        m = compute_metrics(trades)
        m["symbol"] = symbol
        m["final_step"] = state.current_step
        m["blackouts"] = 1 if state.blackout_until else 0
        m["trades"] = trades  # keep for exit-reason analysis
        results[symbol] = m
    return results


def run_variant(variant: str, symbols: List[str], start, end, size: float):
    print(f"\n>>> Running {variant} ...", flush=True)
    t0 = time.time()
    c = Clone5V7Strategy()
    c.params.symbols = symbols
    # Apply per-pair v7 tuning
    # (decide() внутри вызовет _apply_per_pair_params, поэтому просто гарантируем
    #  наличие в params.symbols)

    if variant == "v7_full":
        use_martingale = True
        use_trailing = True
    elif variant == "v7a_trailing":
        use_martingale = False
        use_trailing = True
    elif variant == "v7b_martingale":
        use_martingale = True
        use_trailing = False
    else:
        raise ValueError(variant)

    out = run_ablation(c, start, end, size, symbols,
                       use_martingale=use_martingale, use_trailing=use_trailing)
    elapsed = time.time() - t0

    # Агрегированные метрики
    all_trades = []
    for sym, m in out.items():
        all_trades.extend(m.get("trades", []))
    agg = compute_metrics(all_trades)
    print(f"    {variant}: {agg['trades']} trades, WR={agg['wr_pct']}%, "
          f"PF={agg['pf']}, PnL=${agg['pnl_usd']:+.2f}, MaxDD=${agg['max_dd_usd']}, "
          f"Sharpe={agg['sharpe_like']} [{elapsed:.1f}s]", flush=True)

    # Per-pair summary (без полных трейдов — экономия памяти)
    per_pair = {}
    for sym, m in out.items():
        per_pair[sym] = {
            "trades": m["trades"],
            "wins": m["wins"],
            "wr_pct": m["wr_pct"],
            "pnl_usd": m["pnl_usd"],
            "pf": m["pf"],
            "max_dd_usd": m["max_dd_usd"],
            "avg_hold_min": m["avg_hold_min"],
            "tp_hits": m["tp_hits"],
            "sl_hits": m["sl_hits"],
            "max_hold_exits": m["max_hold_exits"],
            "final_step": m["final_step"],
            "blackouts": m["blackouts"],
        }

    return {
        "variant": variant,
        "use_martingale": use_martingale,
        "use_trailing": use_trailing,
        "aggregate": agg,
        "per_pair": per_pair,
        "elapsed_sec": round(elapsed, 1),
    }


def main():
    print("=== Clone5 v7 ABLATION — Trailing vs Martingale (29 pairs, 3 months) ===", flush=True)
    print(f"Started: {datetime.now(timezone.utc).isoformat()}", flush=True)

    # Период: 3 мес от самой ранней даты
    end = datetime(2026, 6, 7, 0, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
    size = 1.0  # $1/pos для cross-pair честного масштаба

    symbols = get_pairs(min_bars=4000)
    print(f"Pairs: {len(symbols)}  |  Period: {start.date()} → {end.date()}  |  Size: ${size}", flush=True)
    print(f"Excluded: {EXCLUDE}", flush=True)
    print("=" * 90, flush=True)

    results = {}
    for variant in ["v7_full", "v7a_trailing", "v7b_martingale"]:
        results[variant] = run_variant(variant, symbols, start, end, size)

    # === COMPARISON TABLE ===
    print("\n" + "=" * 90)
    print("COMPARISON (29 pairs, 3 months, $1/pos)")
    print("=" * 90)
    print(f"{'Variant':<20} {'Trades':>7} {'WR%':>6} {'PnL$':>8} {'PF':>6} {'MaxDD$':>7} {'Sharpe':>7} {'TP':>4} {'SL':>4} {'MH':>4}")
    print("-" * 90)
    for v in ["v7_full", "v7a_trailing", "v7b_martingale"]:
        m = results[v]["aggregate"]
        print(f"{v:<20} {m['trades']:>7} {m['wr_pct']:>6} ${m['pnl_usd']:>+7.2f} "
              f"{m['pf']:>6} ${m['max_dd_usd']:>6.2f} {m['sharpe_like']:>7} "
              f"{m['tp_hits']:>4} {m['sl_hits']:>4} {m['max_hold_exits']:>4}")

    # Per-pair cross-table (PF)
    print("\n" + "=" * 110)
    print("PER-PAIR PF (v7a=trailing-only, v7b=martingale-only, v7_full=both)")
    print("=" * 110)
    print(f"{'Pair':<14} {'v7a_Tr':>7} {'v7a_WR':>6} {'v7a_PF':>6} {'v7a_PnL':>8}  "
          f"{'v7b_Tr':>7} {'v7b_WR':>6} {'v7b_PF':>6} {'v7b_PnL':>8}  "
          f"{'v7f_PF':>6} {'v7f_PnL':>8}  {'WINNER':<10}")
    print("-" * 110)
    for sym in symbols:
        try:
            a = results["v7a_trailing"]["per_pair"][sym]
            b = results["v7b_martingale"]["per_pair"][sym]
            f = results["v7_full"]["per_pair"][sym]
        except KeyError:
            continue
        a_n = a.get("trades", 0) if isinstance(a.get("trades"), int) else len(a.get("trades", []))
        b_n = b.get("trades", 0) if isinstance(b.get("trades"), int) else len(b.get("trades", []))
        a_pf = a.get("pf", 0); b_pf = b.get("pf", 0); f_pf = f.get("pf", 0)
        a_wr = a.get("wr_pct", 0); b_wr = b.get("wr_pct", 0)
        a_pnl = a.get("pnl_usd", 0); b_pnl = b.get("pnl_usd", 0); f_pnl = f.get("pnl_usd", 0)
        scores = {"v7a": a_pf, "v7b": b_pf, "v7f": f_pf}
        winner = max(scores, key=scores.get) if max(scores.values()) > 0 else "-"
        print(f"{sym:<14} {a_n:>7} {a_wr:>6} {a_pf:>6} ${a_pnl:>+7.2f}  "
              f"{b_n:>7} {b_wr:>6} {b_pf:>6} ${b_pnl:>+7.2f}  "
              f"{f_pf:>6} ${f_pnl:>+7.2f}  {winner:<10}")

    # Profitable-pairs count
    print("\n=== PROFITABLE PAIRS (PF>=1, trades>=3) ===")
    for v in ["v7a_trailing", "v7b_martingale", "v7_full"]:
        pp = results[v]["per_pair"]
        prof = [s for s, m in pp.items() if m["trades"] >= 3 and m["pf"] >= 1.0 and m["pf"] != 10.0]
        print(f"  {v:<20} {len(prof)}/{len(pp)} profitable "
              f"({', '.join(prof) if prof else '-'})")

    # === VERDICT ===
    print("\n" + "=" * 90)
    print("VERDICT")
    print("=" * 90)
    a = results["v7a_trailing"]["aggregate"]
    b = results["v7b_martingale"]["aggregate"]
    f = results["v7_full"]["aggregate"]
    print(f"Trailing-only (v7a):  PF={a['pf']}  PnL=${a['pnl_usd']:+.2f}  WR={a['wr_pct']}%  Sharpe={a['sharpe_like']}")
    print(f"Martingale-only (v7b):PF={b['pf']}  PnL=${b['pnl_usd']:+.2f}  WR={b['wr_pct']}%  Sharpe={b['sharpe_like']}")
    print(f"Combined (v7_full):   PF={f['pf']}  PnL=${f['pnl_usd']:+.2f}  WR={f['wr_pct']}%  Sharpe={f['sharpe_like']}")

    # Save JSON
    out_dir = Path("/tmp/clones_backtest")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"v7_ablation_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    serial = {}
    for v, data in results.items():
        serial[v] = {
            "variant": data["variant"],
            "use_martingale": data["use_martingale"],
            "use_trailing": data["use_trailing"],
            "aggregate": data["aggregate"],
            "elapsed_sec": data["elapsed_sec"],
            "per_pair": {s: {k: val for k, val in m.items() if k != "trades"} for s, m in data["per_pair"].items()},
        }
    with open(out_file, "w") as fh:
        json.dump(serial, fh, indent=2, default=str)
    print(f"\n✓ Saved: {out_file}", flush=True)


if __name__ == "__main__":
    main()
