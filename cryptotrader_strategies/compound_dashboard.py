#!/usr/bin/env python3
"""
Compound Dashboard — компактный отчёт о текущем состоянии compound системы.

Использование:
    python compound_dashboard.py              # полный dashboard
    python compound_dashboard.py --short      # только ключевые метрики
    python compound_dashboard.py --telegram   # формат для Telegram
"""
import os, sys, json, argparse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/andy/CryptoTrader_main')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader_main/.env')

import psycopg2

# R13 FIX: единый источник DSN — db_safe.db_dsn()
from cryptotrader_strategies.db_safe import db_dsn
DB = db_dsn()
STATE_PATH = Path('/home/andy/CryptoTrader_main/compound_state.json')


def get_balance():
    try:
        from cryptotrader_strategies.bybit_safe import bybit_exchange
        ex = bybit_exchange(with_auth=True)
        bal = ex.fetch_balance({'accountType': 'UNIFIED'})
        usdt = bal.get('USDT') or {}
        free = float(usdt.get('free', 0.0) or 0.0)
        total = float(usdt.get('total', 0.0) or 0.0)
        return free, total
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('compound_dashboard.get_balance failed: %s', e)
        return 0.0, 0.0


def get_v7_positions():
    conn = psycopg2.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT symbol, side, unrealized_pnl, opened_at
            FROM positions
            WHERE exchange='bybit' AND status='open'
              AND notes LIKE '%clone5_v7%'
        """)
        return cur.fetchall()
    finally:
        conn.close()


def get_v7_recent_pnl(days=7):
    conn = psycopg2.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT
              COUNT(*) as n,
              SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
              SUM(realized_pnl) as pnl,
              AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100 as wr
            FROM positions
            WHERE exchange='bybit'
              AND status='closed'
              AND notes LIKE '%clone5_v7%'
              AND closed_at > NOW() - INTERVAL '{days} days'
        """)
        row = cur.fetchone()
        if not row:
            return (0, 0, 0.0, 0.0)
        return (row[0] or 0, row[1] or 0, row[2] or 0.0, row[3] or 0.0)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--short', action='store_true')
    parser.add_argument('--telegram', action='store_true')
    args = parser.parse_args()

    free, total = get_balance()
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {'current_pos_usdt': 5.0, 'tier': 'starter'}
    positions = get_v7_positions()
    n, wins, pnl, wr = get_v7_recent_pnl(7)

    # [06.08.2026] Лимит позиций больше не константа 2 — он динамический
    # (compound_engine масштабирует его от баланса). Раньше отчёт печатал "× 2 max"
    # и "0/2" независимо от реальной настройки и вводил в заблуждение.
    pos_usd = float(state.get('current_pos_usdt', 5))
    max_pos = int(state.get('current_max_positions', 3) or 3)

    if args.telegram:
        # Компактный формат: одна строка — один факт, без пустых секций
        lines = [
            f"💰 **Compound** `{state.get('last_update', '')[:16]}`",
            f"Баланс: **${free:.2f}** (всего ${total:.2f})",
            f"Схема: **{max_pos} × ${pos_usd:.2f}** = ${max_pos * pos_usd:.2f} в рынке",
            f"Позиций: **{len(positions)}/{max_pos}**",
        ]
        for sym, side, p, o in positions:
            p_val = p or 0
            try:
                opened_str = o.strftime('%d.%m %H:%M') if o else '—'
            except Exception:
                opened_str = '—'
            lines.append(f"  • {sym} {side} {opened_str} {p_val:+.2f}$")
        lines.append(f"7д: {n} сделок, WR {wr:.0f}%, PnL {pnl:+.2f}$")
        print("\n".join(lines))
        return

    if args.short:
        print(f"${free:.2f} | {max_pos} × ${pos_usd:.2f} | открыто {len(positions)}/{max_pos} | "
              f"7д: {n} сделок, WR {wr:.0f}%, {pnl:+.2f}$")
        return
    print(f"  COMPOUND DASHBOARD  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", flush=True)
    print("="*70, flush=True)
    print(f"  Bybit USDT free:   ${free:.2f}", flush=True)
    print(f"  Bybit USDT total:  ${total:.2f}", flush=True)
    print(f"  Режим:             {state.get('tier', '—')}", flush=True)
    print(f"  Размер сделки:     ${pos_usd:.2f}", flush=True)
    print(f"  Максимум в рынке:  ${max_pos * pos_usd:.2f} ({max_pos} позиций)", flush=True)
    print("-"*70, flush=True)
    print(f"  Open positions:    {len(positions)}/{max_pos}", flush=True)
    for sym, side, p, o in positions:
        sign = '+' if (p or 0) >= 0 else ''
        print(f"    {sym:<10} {side:<6} PnL {sign}${(p or 0):.2f}  opened {o.strftime('%m-%d %H:%M')}", flush=True)
    print("-"*70, flush=True)
    print(f"  Last 7 days: {n} trades, WR {wr:.0f}%, PnL ${pnl:+.2f}", flush=True)
    if n and n > 0:
        pf_est = (wins * 0.005) / ((n - wins) * 0.005) if n > wins else 10.0
        print(f"  Est. PF:           {pf_est:.2f}", flush=True)
    print(f"  Last rebalance:    {state.get('last_update', 'never')}", flush=True)
    print("="*70, flush=True)


if __name__ == "__main__":
    main()
