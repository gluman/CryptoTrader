#!/bin/bash
# scan_and_execute_tg.sh — Telegram-compact wrapper for scan_and_execute.py
#
# ЗАЧЕМ: Cron 76260dbf8e29 deliver=telegram. Полный stdout scan_and_execute.py
#   = 1900+ символов лога каждые 5m = спам в Telegram (288 msg/day).
#   Этот wrapper: запускает scanner, полный лог → файл, в stdout → компактный
#   информативный Telegram-friendly summary.
#
# ФОРМАТ (v3 — 27.06.2026, по запросу Босса "наиболее наглядный с позициями и PnL"):
#   Приоритет — ДЕНЬГИ:
#     1. Открытые позиции (entry/last/SL/TP/uPnL/R:R) — если есть
#     2. P&L за 24h (closed trades, gross + winrate)
#     3. Баланс (equity/free)
#   Убрано (служебное, не нужно Boss'у в каждом тике):
#     • vol_regime / bb_width / trailing state (regime metrics)
#     • API key label (служебное)
#     • LLM decisions при 0 signals (шум)
#     • per-pair trends emoji (regime context — доступно в monitor)
#     • Duration / phase timings
#   Остаётся только если есть РЕАЛЬНОЕ событие:
#     • Новая сделка (OPEN LONG/SHORT)
#     • SL/TP hit (закрытие)
#     • trailing move
#     • Ошибка
#
# ВЫЗОВ: cron 76260dbf8e29 (every 5m, deliver=telegram:519881679)
#───────────────────────────────────────────────────────────────────────────────
set -e
cd /home/andy/CryptoTrader_main

LOG_DIR="$HOME/.hermes/cron/output/scan_full_logs"
mkdir -p "$LOG_DIR"
STAMP=$(date +%Y%m%d_%H%M)_msk
FULL_LOG="$LOG_DIR/scan_${STAMP}.log"

# Run scanner, capture ALL output to full log
/home/andy/cryptotrader-venv/bin/python cryptotrader_strategies/scan_and_execute.py > "$FULL_LOG" 2>&1 || true

# Run aggregate PnL manager (rules: BE/TP/SL thresholds by total uPnL)
PNL_LOG_DIR="$HOME/.hermes/cron/output/aggregate_pnl_logs"
mkdir -p "$PNL_LOG_DIR"
PNL_STAMP=$(date +%Y%m%d_%H%M)_msk
PNL_LOG="$PNL_LOG_DIR/pnl_${PNL_STAMP}.log"
{
  echo "=== aggregate_pnl_manager $(date +%Y-%m-%dT%H:%M:%S%z) MSK ==="
  /home/andy/cryptotrader-venv/bin/python cryptotrader_strategies/aggregate_pnl_manager.py 2>&1
} > "$PNL_LOG" 2>&1 || true

# Extract data from captured output and emit compact Telegram format
/home/andy/cryptotrader-venv/bin/python - "$FULL_LOG" "$PNL_LOG" << 'PYEOF'
import json, re, sys, subprocess, os
from datetime import datetime, timezone, timedelta

log_path = sys.argv[1]
pnl_log_path = sys.argv[2]
with open(log_path) as f:
    raw = f.read()
try:
    with open(pnl_log_path) as f:
        pnl_raw = f.read()
except Exception:
    pnl_raw = ""

lines = raw.split("\n")

# ═══════════════════════════════════════════════════════════════════════════════
# PARSE: JSON summary (last { ... } block)
# ═══════════════════════════════════════════════════════════════════════════════
json_start = None
for i in range(len(lines) - 1, -1, -1):
    if lines[i].strip().startswith("{"):
        json_start = i
        break

summary = None
if json_start is not None:
    json_text = "\n".join(lines[json_start:])
    try:
        summary = json.loads(json_text)
    except json.JSONDecodeError:
        depth = 0
        end = 0
        for j, ch in enumerate(json_text):
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        try:
            summary = json.loads(json_text[:end])
        except Exception:
            summary = None

# [06.08.2026 Босс] В шапке — время ПУБЛИКАЦИИ отчёта. Раньше бралось ts_msk из
# summary скана, а это момент последнего сигнала: отчёт от 14:50 показывал 12:35
# и выглядел как зависший.
ts = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%d.%m %H:%M MSK")
status = summary.get("status", "?") if summary else "?"
icon = "✅" if status == "ok" else "⚠️"

# ═══════════════════════════════════════════════════════════════════════════════
# FETCH LIVE STATE (Bybit + DB) — самый важный блок
# ═══════════════════════════════════════════════════════════════════════════════
sys.path.insert(0, "/home/andy/CryptoTrader_main")
os.chdir("/home/andy/CryptoTrader_main")
from dotenv import load_dotenv
load_dotenv("/home/andy/CryptoTrader_main/.env")

import ccxt, psycopg2

# --- Bybit live (uses select_bybit_key for IP-aware VPN-on/off fallback) ---
try:
    from src.core.bybit_key_selector import select_bybit_key
    api_key, api_secret = select_bybit_key()
    if not api_key or not api_secret:
        raise RuntimeError("bybit_key_selector: ALL keys failed — execution disabled")
    ex = ccxt.bybit({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "linear"}
    })
    ex.load_time_difference()
    bal = ex.fetch_balance(params={"type": "swap", "accountType": "UNIFIED"})
    usdt = bal.get("USDT", {})
    equity = float(usdt.get("total", 0))
    free = float(usdt.get("free", 0))
    used = float(usdt.get("used", 0))
    live_positions = ex.fetch_positions(params={"category": "linear"})
except Exception as e:
    equity = free = used = 0
    live_positions = []
    print(f"{icon} **Scan v9** `{ts}`  ⚠️ Bybit error: {e}")
    sys.exit(0)

# --- DB positions + 24h P&L ---
try:
    conn = psycopg2.connect(host="127.0.0.1", port=5432, database="cryptotrader",
                            user="cryptotrader", password="cryptotrader123")
    cur = conn.cursor()
    # Open positions
    cur.execute("""
        SELECT symbol, side, entry_price, quantity, stop_loss, take_profit,
               unrealized_pnl, opened_at
        FROM positions WHERE status='OPEN' AND market_type='linear'
        ORDER BY symbol
    """)
    db_open = cur.fetchall()
    # 24h closed
    cur.execute("""
        SELECT symbol, side, entry_price, close_price, realized_pnl,
               realized_pnl_percent, closed_at AT TIME ZONE 'UTC' AS closed_at_utc, notes
        FROM positions
        WHERE (status='CLOSED' OR status='closed')
          AND closed_at > NOW() - INTERVAL '24 hours'
        ORDER BY closed_at DESC
    """)
    db_closed_24h = cur.fetchall()
    cur.close(); conn.close()
except Exception as e:
    db_open = []
    db_closed_24h = []
    print(f"{icon} **Scan v9** `{ts}`  ⚠️ DB error: {e}")
    sys.exit(0)

# Конвертируем closed_at в MSK-aware datetime (psycopg2 даёт naive UTC, делаем aware)
from datetime import timezone as _tz, timedelta as _td
_msk_tz = _tz(_td(hours=3))
def _to_msk(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)  # naive → UTC
    return dt.astimezone(_msk_tz)
_db_closed_24h_normalized = []
for r in db_closed_24h:
    sym, side, entry, close, pnl, pct, ts_close, notes = r
    _db_closed_24h_normalized.append((sym, side, entry, close, pnl, pct, _to_msk(ts_close), notes))
db_closed_24h = _db_closed_24h_normalized

# --- Bybit closed trades last 24h (fallback when DB has no records) ---
bybit_closed_24h = []
# Track which symbols we already fetched trades for
_bybit_trades_cache = {}
try:
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    since_ms_24h = int((_dt.now(_tz.utc) - _td(hours=24)).timestamp() * 1000)
    # For "matching buy" we need a wider window (positions opened days ago)
    since_ms_7d = int((_dt.now(_tz.utc) - _td(days=7)).timestamp() * 1000)
    closed_orders = ex.fetch_closed_orders(symbol=None, since=since_ms_24h, limit=50,
                                            params={"category": "linear"})
    for o in closed_orders:
        side_op = o.get("side", "")
        if side_op == "buy":
            continue  # openings не считаем "closed"
        sym = o.get("symbol", "")
        qty = float(o.get("amount") or 0)
        price = float(o.get("price") or 0)
        # [06.08.2026] Было `ts` — затирало время публикации из шапки отчёта
        # (объявлено выше на уровне модуля). Локальной переменной своё имя.
        o_ts = o.get("datetime")
        bybit_closed_24h.append({
            "symbol": sym,
            "qty": qty,
            "price": price,
            "datetime": o_ts,
            "order_id": o.get("id"),
        })
except Exception:
    pass

# Если DB пуста, fallback к Bybit. Используем trades за 7d для matching buy.
db_closed_bybit_24h = []  # Только Bybit-fallback сделки (когда DB пустая)
if not db_closed_24h and bybit_closed_24h:
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    since_ms_7d = int((_dt.now(_tz.utc) - _td(days=7)).timestamp() * 1000)
    for c in bybit_closed_24h[:5]:
        sym = c["symbol"]
        try:
            # Один fetch per symbol
            if sym not in _bybit_trades_cache:
                _bybit_trades_cache[sym] = ex.fetch_my_trades(
                    symbol=sym, since=since_ms_7d, limit=50,
                    params={"category": "linear"})
            trades = _bybit_trades_cache[sym]
            sell_t = next((t for t in trades if t.get("order") == c["order_id"]), None)
            if not sell_t:
                continue
            # Последний BUY перед этим SELL (может быть сильно раньше)
            buys = [t for t in trades
                    if t.get("side") == "buy"
                    and t.get("datetime") and sell_t.get("datetime")
                    and t["datetime"] < sell_t["datetime"]]
            if not buys:
                continue
            buy_t = buys[-1]
            entry = float(buy_t["price"])
            close = float(sell_t["price"])
            qty = float(sell_t["amount"])
            gross = (close - entry) * qty
            fees = float((sell_t.get("fee") or {}).get("cost", 0)) + float((buy_t.get("fee") or {}).get("cost", 0))
            pnl = gross - fees
            pct = (close - entry) / entry * 100 if entry else 0
            ts_close = sell_t.get("datetime")
            try:
                if isinstance(ts_close, str):
                    dt_close = _dt.fromisoformat(ts_close.replace("Z", "+00:00"))
                else:
                    dt_close = ts_close
            except Exception:
                dt_close = None
            db_closed_bybit_24h.append((c["symbol"].replace("/USDT:USDT", ""), "long",
                                   entry, close, pnl, pct, dt_close, "bybit_fallback"))
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════════════════════
# PARSE: events from log (OPEN, CLOSE, SL hit, trailing move, errors)
# ═══════════════════════════════════════════════════════════════════════════════
events = []
errors = []
for line in lines:
    s = line.strip()
    if ("OPEN LONG" in s or "OPEN SHORT" in s) and "clone5" in s:
        events.append(("📥", s))
    elif "SL hit" in s or "TP hit" in s:
        events.append(("🎯", s))
    elif "Trailing" in s and ("ACTIVATED" in s or "MOVED" in s):
        events.append(("📈", s))
    elif "Error" in s or "FAILED" in s:
        errors.append(s[:100])

# ═══════════════════════════════════════════════════════════════════════════════
# RENDER — моноширинный формат (для Telegram)
# ═══════════════════════════════════════════════════════════════════════════════

print(f"✅ v9 {ts}")

# ── OPEN POSITIONS (моноширинный) ─────────────────────────────────────────────
if live_positions:
    pos_rows = []
    total_upnl = 0.0
    for p in live_positions:
        info = p.get("info", {})
        if float(p.get("contracts") or 0) <= 0:
            continue
        sym = info.get("symbol", "").replace("/USDT:USDT", "").replace("USDT", "")
        side = "B" if info.get("side", "").lower() == "buy" else "S"
        upnl = float(info.get("unrealisedPnl", 0))
        total_upnl += upnl
        upnl_str = f"+{upnl:.2f}" if upnl >= 0 else f"{upnl:.2f}"
        pos_rows.append((sym, side, upnl_str))
    
    if pos_rows:
        sign = "+" if total_upnl >= 0 else ""
        print()
        print("```")
        print("Пара      S/L  uPnL")
        for sym, side, upnl_str in pos_rows:
            print(f"{sym:<10}{side:<5}{upnl_str}$")
        print(f"Σ              {sign}{total_upnl:.2f}$")
        print("```")

# ── CLOSED TRADES (за последний час) ─────────────────────────────────────────
try:
    conn2 = psycopg2.connect(host="127.0.0.1", port=5432, database="cryptotrader",
                            user="cryptotrader", password="cryptotrader123")
    cur2 = conn2.cursor()
    cur2.execute("""
        SELECT symbol, side, realized_pnl, closed_at AT TIME ZONE 'UTC' AS closed_at_utc
        FROM positions
        WHERE (status='CLOSED' OR status='closed')
          AND closed_at > NOW() - INTERVAL '1 hour'
        ORDER BY closed_at DESC
    """)
    trades_1h = cur2.fetchall()
    cur2.close()
    conn2.close()
    
    if trades_1h:
        print()
        print("```")
        print("Закрыты за час:")
        print("Пара      S/L  PnL      Время")
        for sym, side, pnl, closed_at in trades_1h:
            if closed_at and closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
            side_str = "B" if side == "long" else "S"
            pnl_val = float(pnl or 0)
            pnl_str = f"+{pnl_val:.2f}" if pnl_val >= 0 else f"{pnl_val:.2f}"
            time_str = closed_at.astimezone(timezone(timedelta(hours=3))).strftime("%H:%M")
            print(f"{sym:<10}{side_str:<5}{pnl_str}$    {time_str}")
        total_1h = sum(float(t[2] or 0) for t in trades_1h)
        sign = "+" if total_1h >= 0 else ""
        print(f"Σ: {sign}{total_1h:.2f}$ ({len(trades_1h)} сделок)")
        print("```")

except Exception as e:
    pass  # Тихо пропускаем если ошибка БД

# ── BALANCE (моноширинный) ────────────────────────────────────────────────────
print()
print("```")
print(f"💰 {equity:.2f}$")
print(f"   free:   {free:.2f}$")
print(f"   locked: {used:.2f}$")
print("```")

# ── PnL ПО ПЕРИОДАМ (3h, 24h, 3d, 7d) ───────────────────────────────────────
periods = [
    ("3h", "3 hours"),
    ("8h", "8 hours"),
    ("24h", "24 hours"),
    ("3d", "3 days"),
    ("7d", "7 days"),
]

pnl_by_period = {}
try:
    conn = psycopg2.connect(host="127.0.0.1", port=5432, database="cryptotrader",
                            user="cryptotrader", password="cryptotrader123")
    cur = conn.cursor()
    for label, interval in periods:
        cur.execute(f"""
            SELECT COALESCE(SUM(realized_pnl), 0)
            FROM positions
            WHERE (status='CLOSED' OR status='closed')
              AND closed_at > NOW() - INTERVAL '{interval}'
        """)
        pnl_by_period[label] = float(cur.fetchone()[0] or 0)
    cur.close()
    conn.close()
except Exception:
    for label, _ in periods:
        pnl_by_period[label] = 0.0

print()
print("```")
print("Период  PnL")
for label, _ in periods:
    pnl = pnl_by_period[label]
    pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
    print(f"{label:<8}{pnl_str}$")
print("```")
PYEOF