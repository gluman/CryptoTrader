#!/usr/bin/env python3
"""
Walk-Forward Validation для v7a (trailing-only).

Train: 6 нед (42 дня). Test: 2 нед (14 дней). Step: 1 нед (7 дней).
Пары: 4 (TON, NEAR, SUI, SOL) — top edge по OOS 14d.
Size: $1/pos.
Период: 2026-03-07 → 2026-06-07 (138 дней = ~19 нед).

WF logic:
  For each window i:
    train_start = start + i*step
    train_end   = train_start + train_window
    test_start  = train_end
    test_end    = test_start + test_window
    Run v7a on train, get params (none — fixed)
    Run v7a on test, get metrics
    Compare test_pf vs train_pf (should not collapse)

Цель: подтвердить, что edge держится вне обучающей выборки.
"""
from __future__ import annotations

import os, sys, json, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import psycopg2
import numpy as np

from cryptotrader_strategies.clone_5_v7 import Clone5V7Strategy
from cryptotrader_strategies.run_backtest import load_ohlcv, _simulate_trade, compute_metrics, TradeResult

DB = dict(host="192.168.0.149", port=5432, database="cryptotrader",
          user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"])

# WF config
TRAIN_DAYS = 42   # 6 weeks
TEST_DAYS = 14    # 2 weeks
STEP_DAYS = 7     # 1 week slide
START = datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 6, 7, 0, 0, 0, tzinfo=timezone.utc)
SYMBOLS = ["TONUSDT", "NEARUSDT", "SUIUSDT", "SOLUSDT"]


def run_window(strategy, start, end, symbols, label):
    """Run v7a on [start, end] for given symbols. Return aggregate + per-pair metrics."""
    all_trades = []
    per_pair = {}
    for sym in symbols:
        df = load_ohlcv(sym, "5m", start, end)
        if df.empty or len(df) < 60:
            per_pair[sym] = {"trades": 0, "pf": 0, "pnl_usd": 0, "wr_pct": 0}
            continue
        # Reset state
        state = strategy._get_state(sym)
        state.consecutive_losses = 0
        state.current_step = 0
        state.daily_pnl_today = 0.0
        state.daily_date = None
        state.blackout_until = None

        trades = []
        i = 60
        tf_minutes = 5
        max_bars = int(strategy.params.max_hold_minutes / tf_minutes)
        while i < len(df) - 1:
            h = df.iloc[:i + 1]
            ts = h.index[-1]
            if state.is_blackout(ts) or state.daily_loss_exceeded():
                i += 1; continue
            d = strategy.decide(h, sym)
            sig = d.get("signal")
            if sig in ("BUY", "SELL") and d.get("confidence", 0) >= strategy.params.min_confidence and d.get("side") and d.get("stop_loss_pct", 0) > 0:
                t = _simulate_trade(
                    df=df, entry_idx=i, side=d["side"],
                    entry_price=float(df.iloc[i]["close"]), entry_time=ts,
                    sl_pct=d["stop_loss_pct"], tp_pct=d["take_profit_pct"],
                    max_bars=max_bars, tf_minutes=tf_minutes,
                    strategy=strategy, size_usdt=1.0,
                )
                trades.append(t)
                if t.pnl_dollar > 0: state.record_win(t.exit_time, t.pnl_dollar)
                else: state.record_loss(t.exit_time, t.pnl_dollar)
                i = t.exit_idx + 1
            else:
                i += 1
        m = compute_metrics(trades)
        m["symbol"] = sym
        per_pair[sym] = m
        all_trades.extend(trades)
    agg = compute_metrics(all_trades)
    return agg, per_pair, all_trades


def main():
    print("=== Walk-Forward Validation: v7a@30min ===", flush=True)
    print(f"Started: {datetime.now(timezone.utc).isoformat()}", flush=True)
    print(f"Train: {TRAIN_DAYS}d | Test: {TEST_DAYS}d | Step: {STEP_DAYS}d", flush=True)
    print(f"Pairs: {SYMBOLS}", flush=True)
    print(f"Period: {START.date()} → {END.date()} ({(END-START).days} days)", flush=True)
    print("=" * 110, flush=True)

    # Build windows
    windows = []
    i = 0
    cur = START
    while True:
        train_start = cur
        train_end = train_start + timedelta(days=TRAIN_DAYS)
        test_start = train_end
        test_end = test_start + timedelta(days=TEST_DAYS)
        if test_end > END:
            break
        windows.append((i, train_start, train_end, test_start, test_end))
        cur = cur + timedelta(days=STEP_DAYS)
        i += 1

    print(f"Total windows: {len(windows)}", flush=True)
    print("=" * 110, flush=True)

    results = []
    t_total = time.time()
    for idx, t_start, t_end, te_start, te_end in windows:
        print(f"\n--- Window {idx}: train {t_start.date()}→{t_end.date()} | "
              f"test {te_start.date()}→{te_end.date()} ---", flush=True)
        wt = time.time()
        # Train
        s = Clone5V7Strategy()
        s.params.symbols = SYMBOLS
        train_agg, train_pp, _ = run_window(s, t_start, t_end, SYMBOLS, "train")
        # Test
        s2 = Clone5V7Strategy()
        s2.params.symbols = SYMBOLS
        test_agg, test_pp, _ = run_window(s2, te_start, te_end, SYMBOLS, "test")
        wt_elapsed = time.time() - wt

        # Compute drift
        pf_drift = (test_agg['pf'] - train_agg['pf']) if train_agg['pf'] > 0 else 0
        pnl_drift = test_agg['pnl_usd'] - train_agg['pnl_usd']
        wr_drift = test_agg['wr_pct'] - train_agg['wr_pct']
        verdict = "✓" if (test_agg['pf'] >= 0.7 * train_agg['pf'] and test_agg['pnl_usd'] > -0.05) else "⚠"

        print(f"  TRAIN: tr={train_agg['trades']:>3}  WR={train_agg['wr_pct']:>5}%  PF={train_agg['pf']:>5}  "
              f"PnL=${train_agg['pnl_usd']:>+6.2f}  Sharpe={train_agg['sharpe_like']:>5}", flush=True)
        print(f"  TEST:  tr={test_agg['trades']:>3}  WR={test_agg['wr_pct']:>5}%  PF={test_agg['pf']:>5}  "
              f"PnL=${test_agg['pnl_usd']:>+6.2f}  Sharpe={test_agg['sharpe_like']:>5}", flush=True)
        print(f"  DRIFT: PF {pf_drift:+.2f}  PnL ${pnl_drift:+.2f}  WR {wr_drift:+.1f}%  {verdict}  "
              f"[{wt_elapsed:.0f}s]", flush=True)

        results.append({
            "window": idx,
            "train_start": t_start.isoformat(),
            "train_end": t_end.isoformat(),
            "test_start": te_start.isoformat(),
            "test_end": te_end.isoformat(),
            "train_agg": train_agg,
            "test_agg": test_agg,
            "train_per_pair": train_pp,
            "test_per_pair": test_pp,
            "drift_pf": pf_drift,
            "drift_pnl": pnl_drift,
            "drift_wr": wr_drift,
            "verdict": verdict,
        })

    total_elapsed = time.time() - t_total
    print(f"\n=== TOTAL: {total_elapsed:.0f}s ===", flush=True)

    # === COMPARISON TABLE ===
    print("\n" + "=" * 110)
    print("WALK-FORWARD COMPARISON TABLE")
    print("=" * 110)
    print(f"{'W#':>3} {'Train Period':<25} {'Test Period':<25} {'T_PF':>5} {'T_PnL':>7} {'T_WR':>5}  "
          f"{'E_PF':>5} {'E_PnL':>7} {'E_WR':>5}  {'DriftPF':>8}  {'Verdict':<6}")
    print("-" * 110)
    for r in results:
        tp = f"{r['train_start'][:10]}→{r['train_end'][:10]}"
        ep = f"{r['test_start'][:10]}→{r['test_end'][:10]}"
        print(f"{r['window']:>3} {tp:<25} {ep:<25} "
              f"{r['train_agg']['pf']:>5.2f} ${r['train_agg']['pnl_usd']:>+6.2f} {r['train_agg']['wr_pct']:>5}  "
              f"{r['test_agg']['pf']:>5.2f} ${r['test_agg']['pnl_usd']:>+6.2f} {r['test_agg']['wr_pct']:>5}  "
              f"{r['drift_pf']:>+8.2f}  {r['verdict']:<6}")

    # Per-window per-pair test results
    print("\n" + "=" * 110)
    print("PER-WINDOW TEST RESULTS BY PAIR")
    print("=" * 110)
    for sym in SYMBOLS:
        print(f"\n  {sym}:")
        for r in results:
            m = r['test_per_pair'].get(sym, {})
            tr = m.get('trades', 0)
            pf = m.get('pf', 0)
            pnl = m.get('pnl_usd', 0)
            wr = m.get('wr_pct', 0)
            print(f"    W{r['window']}: tr={tr:>3}  PF={pf:>5.2f}  PnL=${pnl:>+6.2f}  WR={wr:>5}%")

    # Summary stats
    print("\n" + "=" * 80)
    print("WALK-FORWARD VERDICT")
    print("=" * 80)
    test_pfs = [r['test_agg']['pf'] for r in results if r['test_agg']['trades'] > 0]
    test_pnls = [r['test_agg']['pnl_usd'] for r in results]
    positive_windows = sum(1 for r in results if r['test_agg']['pnl_usd'] > 0)
    total_test_pnl = sum(test_pnls)
    avg_test_pf = np.mean(test_pfs) if test_pfs else 0
    median_test_pf = np.median(test_pfs) if test_pfs else 0

    print(f"Windows: {len(results)}")
    print(f"Test PF — mean: {avg_test_pf:.2f}, median: {median_test_pf:.2f}")
    print(f"Test PnL — total: ${total_test_pnl:+.2f}, positive: {positive_windows}/{len(results)}")
    print(f"Test Sharpe — mean: {np.mean([r['test_agg']['sharpe_like'] for r in results]):.2f}")

    if median_test_pf >= 1.0 and total_test_pnl > 0:
        print(f"\n✅ WF PASS: edge stable across {len(results)} windows (median PF={median_test_pf:.2f})")
    elif median_test_pf >= 0.8 and positive_windows >= len(results) * 0.6:
        print(f"\n⚠️ WF SOFT: edge weak but positive (median PF={median_test_pf:.2f}, {positive_windows}/{len(results)} profitable)")
    else:
        print(f"\n❌ WF FAIL: edge collapses OOS (median PF={median_test_pf:.2f}, {positive_windows}/{len(results)} profitable)")

    # Save
    out_dir = Path("/tmp/clones_backtest")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"v7a_wf_validation_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(out_file, "w") as fh:
        json.dump({
            "config": {
                "train_days": TRAIN_DAYS, "test_days": TEST_DAYS, "step_days": STEP_DAYS,
                "symbols": SYMBOLS, "start": START.isoformat(), "end": END.isoformat(),
            },
            "summary": {
                "windows": len(results),
                "median_test_pf": float(median_test_pf),
                "mean_test_pf": float(avg_test_pf),
                "total_test_pnl": float(total_test_pnl),
                "positive_windows": positive_windows,
            },
            "results": results,
            "elapsed_sec": round(total_elapsed, 1),
        }, fh, indent=2, default=str)
    print(f"\n✓ Saved: {out_file}", flush=True)


if __name__ == "__main__":
    main()
