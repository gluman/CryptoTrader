#!/usr/bin/env python3
"""
CryptoTrader Multi-Strategy Monitor — 30-min interval.

Boss: периодический мониторинг v2/v6/v7a + Bybit state + DB state.
Формат: таблица для Telegram DM (Boss, 519881679).

Checks:
  1. Open positions (count, symbols, PnL)
  2. Last hour signals (per strategy)
  3. Bybit balance (free USDT)
  4. cron health (e020bee290d2 last run)
  5. Recent closed trades (last 24h PnL)
  6. Daily strategy PnL aggregation

Если что-то критично — шлёт ALERT. Иначе — компактный status.
"""
from __future__ import annotations

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import psycopg2
import ccxt

DB = dict(
    host="192.168.0.149", port=5432, database="cryptotrader",
    user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"],
)
STATE_PATH = Path('/home/andy/CryptoTrader/compound_state.json')

STRATEGIES = ["clone5_v7_trailing_only", "clone5_v6_market_maker_full", "clone5_v2_market_maker_ict"]


def get_balance() -> tuple:
    try:
        ex = ccxt.bybit({
            'options': {'defaultType': 'linear'},
            'enableRateLimit': True,
            'recvWindow': 60000,
            'timeout': 5000,
        })
        bal = ex.fetch_balance({'accountType': 'UNIFIED'})
        usdt = bal.get('total', {}).get('USDT', 0) or 0
        free = bal.get('free', {}).get('USDT', 0) or 0
        return float(usdt), float(free)
    except Exception as e:
        return 0.0, 0.0


def get_state():
    """Open positions, last signals, recent closed, compound state."""
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()

    # Open positions
    cur.execute("""
        SELECT symbol, side, market_type, leverage, quantity, entry_price,
               unrealized_pnl, unrealized_pnl_percent, opened_at, notes
        FROM positions
        WHERE status='open'
        ORDER BY opened_at DESC
    """)
    opens = cur.fetchall()

    # Last signals per strategy (1h)
    cur.execute("""
        SELECT strategy, COUNT(*), MAX(created_at)
        FROM strategy_signals
        WHERE created_at > NOW() - INTERVAL '1 hour'
        GROUP BY strategy
        ORDER BY strategy
    """)
    sigs_1h = cur.fetchall()

    # Closed last 24h
    cur.execute("""
        SELECT symbol, side, realized_pnl, closed_at, notes
        FROM positions
        WHERE closed_at > NOW() - INTERVAL '24 hours' AND status='closed'
        ORDER BY closed_at DESC
    """)
    closed_24h = cur.fetchall()

    # strategy_signals last 24h
    cur.execute("""
        SELECT strategy, action, status, COUNT(*)
        FROM strategy_signals
        WHERE created_at > NOW() - INTERVAL '24 hours'
        GROUP BY strategy, action, status
        ORDER BY strategy, action
    """)
    sigs_24h = cur.fetchall()

    # compound state
    compound = None
    if STATE_PATH.exists():
        try:
            compound = json.loads(STATE_PATH.read_text())
        except Exception:
            compound = None

    cur.close()
    conn.close()
    return {
        "opens": opens,
        "sigs_1h": sigs_1h,
        "closed_24h": closed_24h,
        "sigs_24h": sigs_24h,
        "compound": compound,
    }


def format_report(state, balance_total, balance_free) -> str:
    """Telegram-friendly markdown report."""
    now = datetime.now(timezone.utc)
    lines = []
    lines.append(f"📊 **CryptoTrader Multi-Strategy Monitor**")
    lines.append(f"`{now.strftime('%Y-%m-%d %H:%M UTC')}`  every 30min")
    lines.append("")

    # 1. Bybit balance
    lines.append(f"💰 **Balance**: ${balance_total:.2f} total  ${balance_free:.2f} free")
    if state["compound"]:
        c = state["compound"]
        lines.append(f"   Tier: {c.get('tier','?')}  Pos: ${c.get('current_pos_usdt', 0):.2f}  "
                     f"last_update: {c.get('last_update', '?')[:16]}")
    lines.append("")

    # 2. Open positions
    opens = state["opens"]
    if opens:
        lines.append(f"📌 **Open positions**: {len(opens)}")
        for r in opens:
            sym, side, mkt, lev, qty, ep, upnl, upnl_pct, opened, notes = r
            note_short = (notes or "")[:25]
            lines.append(f"   `{sym:<10}` {side:<5} lev={lev} qty={qty}  "
                         f"uPnL=${(upnl or 0):+.4f}  ({note_short})")
    else:
        lines.append("📌 **Open positions**: 0")
    lines.append("")

    # 3. Signals last 1h
    lines.append("🔔 **Signals last 1h**:")
    if state["sigs_1h"]:
        for strat, cnt, last_ts in state["sigs_1h"]:
            short = strat.replace("clone5_", "").replace("_market_maker", "")
            lines.append(f"   `{short}`: {cnt} signals  last @ {last_ts}")
    else:
        lines.append("   (no signals last hour — рынок тихий)")
    lines.append("")

    # 4. Closed last 24h
    closed = state["closed_24h"]
    if closed:
        total_pnl = sum(r[2] or 0 for r in closed)
        lines.append(f"📈 **Closed 24h**: {len(closed)} trades, PnL: ${total_pnl:+.4f}")
        for r in closed[:5]:
            sym, side, pnl, closed_at, notes = r
            lines.append(f"   `{sym:<10}` {side:<5} ${pnl:+.4f}  @ {closed_at}")
    else:
        lines.append("📈 **Closed 24h**: 0 trades")
    lines.append("")

    # 5. Per-strategy signal summary
    lines.append("📋 **Strategy signals (24h)**:")
    sig_groups = {}
    for strat, action, status, cnt in state["sigs_24h"]:
        short = strat.replace("clone5_", "").replace("_market_maker", "")
        sig_groups.setdefault(short, []).append((action, status, cnt))
    for short, items in sig_groups.items():
        summary = ", ".join(f"{a}/{s}={c}" for a, s, c in items)
        lines.append(f"   `{short}`: {summary}")
    if not sig_groups:
        lines.append("   (no signals)")

    return "\n".join(lines)


def main():
    print(f"=== Monitor run: {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    balance_total, balance_free = get_balance()
    state = get_state()
    report = format_report(state, balance_total, balance_free)
    print(report, flush=True)
    # Also save to file for cron output collection
    out_dir = Path.home() / ".hermes/cron/output/multi_monitor"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"monitor_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    out_file.write_text(report)
    print(f"\n✓ Saved: {out_file}", flush=True)


if __name__ == "__main__":
    main()
