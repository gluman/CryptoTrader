#!/usr/bin/env python3
"""
OOS Live Validator — отслеживает live performance v7 за последние 14 дней.
Сравнивает с OOS ожиданиями: PF 1.76, WR 62.1%, +$0.87 (base $5) за 4 нед.

Использование:
    python oos_live_validator.py          # одноразовая проверка
    python oos_live_validator.py --alert   # отправить алерт если drift > 30%
"""
import os, sys, json, argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import psycopg2

DB = dict(host="192.168.0.149", port=5432, database="cryptotrader",
          user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"])


def get_live_trades(days=14):
    """Получить закрытые позиции v7 за последние N дней."""
    conn = psycopg2.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT symbol, side, realized_pnl, notes, opened_at, closed_at
            FROM positions
            WHERE exchange='bybit'
              AND status='closed'
              AND notes LIKE '%clone5_v7%'
              AND closed_at > NOW() - INTERVAL '{days} days'
            ORDER BY closed_at DESC
        """)
        return cur.fetchall()
    finally:
        conn.close()


def compute_live_metrics(trades):
    if not trades:
        return None
    pnls = [t[2] or 0.0 for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / len(pnls) * 100
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p <= 0))
    pf = gross_w / gross_l if gross_l > 0 else (10.0 if gross_w > 0 else 0.0)
    return {
        'trades': len(pnls),
        'wr_pct': wr,
        'pf': pf,
        'pnl_usd': sum(pnls),
        'wins': wins,
        'losses': len(pnls) - wins,
    }


# === OOS EXPECTATIONS (v7 backtest) ===
OOS_EXPECTED = {
    'wr_pct': 62.1,
    'pf': 1.76,
    'trades_per_4w': 66,  # = 16.5/week, 2.4/day
    'pnl_per_4w': 0.87,   # base $5
}
DRIFT_THRESHOLD = 0.30  # 30% deviation = alert


def check_drift(live, expected, period_days=14):
    """Сравнить live с OOS, вернуть список дрифтов."""
    drifts = []
    scale = period_days / 28.0  # scale to 4-week equivalent
    # WR
    wr_diff = abs(live['wr_pct'] - expected['wr_pct']) / expected['wr_pct']
    if wr_diff > DRIFT_THRESHOLD:
        drifts.append(f"WR drift: live={live['wr_pct']:.1f}% vs OOS={expected['wr_pct']:.1f}% (Δ{wr_diff*100:.0f}%)")
    # PF
    pf_diff = abs(live['pf'] - expected['pf']) / expected['pf']
    if pf_diff > DRIFT_THRESHOLD:
        drifts.append(f"PF drift: live={live['pf']:.2f} vs OOS={expected['pf']:.2f} (Δ{pf_diff*100:.0f}%)")
    # PnL scaled
    expected_pnl_scaled = expected['pnl_per_4w'] * scale
    pnl_diff = abs(live['pnl_usd'] - expected_pnl_scaled)
    if expected_pnl_scaled > 0 and pnl_diff / expected_pnl_scaled > DRIFT_THRESHOLD:
        drifts.append(f"PnL drift: live=${live['pnl_usd']:+.2f} vs expected ${expected_pnl_scaled:+.2f} (Δ${pnl_diff:+.2f})")
    return drifts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--alert', action='store_true')
    parser.add_argument('--days', type=int, default=14)
    args = parser.parse_args()

    print(f"=== OOS Live Validator (last {args.days} days) ===", flush=True)
    print(f"Expected: WR {OOS_EXPECTED['wr_pct']}%, PF {OOS_EXPECTED['pf']}, PnL ${OOS_EXPECTED['pnl_per_4w']*args.days/28:.2f} (scaled)", flush=True)
    print("-"*70, flush=True)

    trades = get_live_trades(args.days)
    if not trades:
        print(f"⏳ NO closed v7 positions in last {args.days} days yet", flush=True)
        print(f"   Need 14+ days of live data for validation. v7 just launched.", flush=True)
        return None, []

    metrics = compute_live_metrics(trades)
    print(f"Live trades:   {metrics['trades']}", flush=True)
    print(f"Live WR:       {metrics['wr_pct']:.1f}% (OOS: {OOS_EXPECTED['wr_pct']}%)", flush=True)
    print(f"Live PF:       {metrics['pf']:.2f} (OOS: {OOS_EXPECTED['pf']})", flush=True)
    print(f"Live PnL:      ${metrics['pnl_usd']:+.2f}", flush=True)
    print("-"*70, flush=True)

    drifts = check_drift(metrics, OOS_EXPECTED, args.days)
    if drifts:
        print(f"⚠️  DRIFT DETECTED ({len(drifts)} metrics off by >{DRIFT_THRESHOLD*100:.0f}%):", flush=True)
        for d in drifts:
            print(f"  • {d}", flush=True)
        if args.alert:
            print(f"\n🚨 ALERT: consider pause v7 live and re-optimize", flush=True)
    else:
        print(f"✅ NO DRIFT — live performance matches OOS expectations", flush=True)
        print(f"   Continue compound", flush=True)

    # Exit reasons (из notes)
    exit_stats = {}
    for t in trades:
        er = t[3] or 'UNKNOWN'
        exit_stats.setdefault(er, {'n': 0, 'pnl': 0})
        exit_stats[er]['n'] += 1
        exit_stats[er]['pnl'] += (t[2] or 0)
    print(f"\nExit reasons:", flush=True)
    for er, st in sorted(exit_stats.items(), key=lambda x: -x[1]['n']):
        wr = sum(1 for t in trades if t[3] == er and (t[2] or 0) > 0) / st['n'] * 100 if st['n'] > 0 else 0
        print(f"  {er:<15}  n={st['n']:>3}  WR={wr:>5.1f}%  PnL=${st['pnl']:+.2f}", flush=True)

    return metrics, drifts


if __name__ == "__main__":
    main()
