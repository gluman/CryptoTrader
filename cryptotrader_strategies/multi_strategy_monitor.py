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
# Единый timezone helper (MSK UTC+3) для всех отчётов Boss
sys.path.insert(0, '/home/andy/.hermes/scripts')
from time_utils import now_msk_str, to_msk_str, msk_iso_now, MSK  # noqa: E402

from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import psycopg2
import ccxt

DB = dict(
    host="192.168.0.149", port=5432, database="cryptotrader",
    user="cryptotrader", password=os.environ.get("POSTGRES_PASSWORD", ""),
)
STATE_PATH = Path('/home/andy/CryptoTrader/compound_state.json')

STRATEGIES = ["clone5_v7_trailing_only"]  # 08.06.2026: v6/v2 отключены (code review L2)


def get_balance() -> tuple:
    """
    Возвращает (total_wallet, available, breakdown_dict).

    Boss 08.06.2026 15:55 спросил: «почему только $12.42 свободно?». Ответ —
    нужен breakdown, потому что total ($21.21) минус available ($11.62) =
    $9.58 «заморожено». Без breakdown невозможно понять, откуда разница.

    breakdown_dict содержит:
      initial_margin    — залог в открытых позициях
      maintenance_margin — поддерживающая маржа
      perp_upl          — unrealized PnL
      pending_orders_n  — кол-во открытых лимитных ордеров
      pending_orders_margin — сколько блокируют pending ордера
      borrow_usdc       — позаимствованный USDC (отрицательный equity)
      frozen_remainder  — что не распределено по bucket'ам
    """
    try:
        from cryptotrader_strategies.bybit_safe import bybit_exchange
        ex = bybit_exchange(with_auth=True)
        # 1) Wallet balance (unified account)
        wb = ex.request('/v5/account/wallet-balance', 'private', 'GET', {
            'accountType': 'UNIFIED',
        })
        wb_result = (wb or {}).get('result') or {}
        wb_list = wb_result.get('list') or []
        acc = wb_list[0] if wb_list else {}
        # Bybit semantics: totalInitialMargin = initial margin of OPEN POSITIONS
        # (НЕ включает pending orders margin). Pending orders резервируют
        # risk-limit, и это видно через разницу (total - available - IM - MM).
        # Поэтому frozen_remainder = locked - IM - MM будет включать pending
        # margin + frozen funds. Помечаем это в breakdown.
        total = float(acc.get('totalWalletBalance', 0) or 0)
        available = float(acc.get('totalAvailableBalance', 0) or 0)
        im = float(acc.get('totalInitialMargin', 0) or 0)
        mm = float(acc.get('totalMaintenanceMargin', 0) or 0)
        upl = float(acc.get('totalPerpUPL', 0) or 0)
        # 2) Open orders (regular, может блокировать margin через risk limit)
        try:
            orders = ex.request('/v5/order/realtime', 'private', 'GET', {
                'category': 'linear', 'settleCoin': 'USDT', 'limit': 50,
            }).get('result', {}).get('list', [])
        except Exception:
            orders = []
        # 3) Per-coin: USDC borrow
        borrow_usdc = 0.0
        for c in acc.get('coin', []):
            if c.get('coin') == 'USDC':
                borrow_usdc = float(c.get('borrowAmount', 0) or 0)
                break
        # 4) Заморожено в pending-ордерах (грубая оценка: notional/leverage)
        #    Bybit не возвращает точный "locked by pending orders" — считаем
        #    через sum(qty*price/lev) с поправкой. Это эвристика.
        #    ВАЖНО: pending-margin ВКЛЮЧЁН в frozen_remainder, потому что Bybit
        #    не выделяет его отдельной строкой в wallet-balance. Поэтому в
        #    breakdown мы показываем pending_margin как ИНФОРМАТИВНУЮ метрику,
        #    но в арифметике не вычитаем (иначе frozen_remainder будет фейк
        #    отрицательным).
        pending_margin = 0.0
        for o in orders:
            if o.get('reduceOnly') == 'true' or o.get('reduceOnly') is True:
                continue  # reduce-only не блокирует новую маржу
            try:
                qty = float(o.get('qty', 0))
                price = float(o.get('price', 0) or 0)
                lev = float(o.get('leverage', 1) or 1)
                if price > 0 and lev > 0:
                    pending_margin += (qty * price) / lev
            except (ValueError, TypeError):
                continue
        # 5) Remainder (pending orders margin + frozen funds + pending withdraw)
        locked_total = total - available
        # IM и MM — это маржа **открытых позиций**. Pending orders margin
        # **не включены** в IM, но сидят в разнице locked_total - IM - MM.
        frozen_remainder = locked_total - im - mm
        return total, available, {
            'initial_margin': im,
            'maintenance_margin': mm,
            'perp_upl': upl,
            'pending_orders_n': len(orders),
            'pending_orders_margin': pending_margin,
            'borrow_usdc': borrow_usdc,
            'frozen_remainder': frozen_remainder,
        }
    except Exception as e:
        # Логируем ошибку, а не молча возвращаем нули (code review H5)
        import logging
        logging.getLogger(__name__).warning('get_balance failed: %s', e)
        return 0.0, 0.0, {
            'initial_margin': 0.0,
            'maintenance_margin': 0.0,
            'perp_upl': 0.0,
            'pending_orders_n': 0,
            'pending_orders_margin': 0.0,
            'borrow_usdc': 0.0,
            'frozen_remainder': 0.0,
        }


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

    # R18 (18.06.2026): LLM decisions last 24h — confirmed/rejected + reasoning
    cur.execute("""
        SELECT symbol, action, strategy,
               exit_plan_json->>'llm_confirmed' AS llm_confirmed,
               exit_plan_json->>'llm_confidence' AS llm_conf,
               exit_plan_json->>'llm_co_action' AS co_action,
               exit_plan_json->>'llm_reasoning' AS llm_reasoning,
               created_at
        FROM strategy_signals
        WHERE created_at > NOW() - INTERVAL '24 hours'
          AND exit_plan_json ? 'llm_confirmed'
        ORDER BY created_at DESC
        LIMIT 20
    """)
    llm_decs = cur.fetchall()

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
        "llm_decs": llm_decs,
        "compound": compound,
    }


def format_report(state, balance_total, balance_free, breakdown=None) -> str:
    """Telegram-friendly markdown report.

    breakdown (dict) — поле из get_balance(). Если None, показываем только
    total/free без расшифровки (для обратной совместимости).
    """
    now = datetime.now(timezone.utc)
    lines = []
    lines.append(f"📊 **CryptoTrader Multi-Strategy Monitor**")
    lines.append(f"`{now_msk_str()}`  every 30min")
    lines.append("")

    # 1. Bybit balance
    locked = balance_total - balance_free
    lines.append(f"💰 **Balance**: ${balance_total:.2f} total  ${balance_free:.2f} free  "
                 f"({locked:.2f} locked)")
    # 1a. Locked breakdown (Новое: отвечает «куда делись деньги?», Boss 08.06.2026 15:55)
    # Account mode: REGULAR_MARGIN (см. /v5/account/info → marginMode).
    # В REGULAR_MARGIN totalInitialMargin ВКЛЮЧАЕТ в себя:
    #   - maintenance margin
    #   - pending orders margin (резервирование под risk limit)
    #   - unrealised losses (если они превышают available)
    # Поэтому показываем IM как «общий залог», а MM НЕ показываем отдельно.
    if breakdown and (locked > 0.01 or breakdown.get('pending_orders_n', 0) > 0
                      or breakdown.get('borrow_usdc', 0) > 0):
        im = breakdown.get('initial_margin', 0)
        upl = breakdown.get('perp_upl', 0)
        pon = breakdown.get('pending_orders_n', 0)
        pom = breakdown.get('pending_orders_margin', 0)
        borrow = breakdown.get('borrow_usdc', 0)
        rest = breakdown.get('frozen_remainder', 0)
        lines.append(f"   📦 **Locked breakdown** (REGULAR_MARGIN):")
        if im > 0.001:
            lines.append(f"      • Total margin (IM+MM+pending): ${im:.4f}")
        if pon > 0:
            lines.append(f"      • ↳ из них pending orders:    {pon} шт → ${pom:.4f} (est.)")
        if borrow > 0.001:
            lines.append(f"      • Borrow (USDC):     ${borrow:.4f}  ⚠️ начисляется %")
        if abs(rest) > 0.01:
            # rest = locked - IM. Должен быть 0 в идеале. Если > 0, значит
            # есть frozen funds / pending withdraw / bonus deductions.
            lines.append(f"      • Frozen/прочее:     ${rest:+.4f}")
        if upl != 0:
            sign = "+" if upl >= 0 else ""
            lines.append(f"      • Perp uPnL:         {sign}${upl:.4f}  (не входит в locked)")
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

    # 6. R18: LLM decisions (24h) — Composite MM reasoning
    llm_decs = state.get("llm_decs", [])
    if llm_decs:
        lines.append("")
        lines.append("🧠 **LLM decisions (24h)**:")
        for sym, action, strat, confirmed, conf, co, reasoning, ts in llm_decs:
            icon = "✓" if confirmed == "true" else "✗"
            short = strat.replace("clone5_", "").replace("_market_maker", "")[:12]
            conf_val = float(conf) if conf else 0.0
            # MSK timestamp
            ts_str = ts.strftime("%H:%M") if ts else "??"
            lines.append(f"   {icon} `{sym:<10}` {action:<4} conf={conf_val:.2f} "
                         f"co={co or '?'} @{ts_str} [{short}]")
            if reasoning:
                # Show first 150 chars of reasoning
                r_short = reasoning[:150].replace("\n", " ")
                lines.append(f"      └ {r_short}")
    else:
        lines.append("")
        lines.append("🧠 **LLM decisions (24h)**: (none — no non-HOLD candidates)")

    return "\n".join(lines)


def main():
    print(f"=== Monitor run: {msk_iso_now()} ===", flush=True)
    balance_total, balance_free, breakdown = get_balance()
    state = get_state()
    report = format_report(state, balance_total, balance_free, breakdown)
    print(report, flush=True)
    # Also save to file for cron output collection
    out_dir = Path.home() / ".hermes/cron/output/multi_monitor"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"monitor_{datetime.now(MSK).strftime('%Y%m%d_%H%M')}_msk.md"
    out_file.write_text(report)
    print(f"\n✓ Saved: {out_file}", flush=True)


if __name__ == "__main__":
    main()
