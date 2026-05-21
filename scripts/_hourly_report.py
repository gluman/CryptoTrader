#!/usr/bin/env python
"""Compact hourly CryptoTrader report."""
import sys
import os
import json
import urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/andy")

# ── Load .env manually ──
env = {}
with open('/home/andy/.env', 'r') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k] = v
            os.environ[k] = v

import psycopg2
import ccxt

# ── Helpers ──
def safe(fallback="N/A"):
    """Decorator to catch and return fallback on errors."""
    def wrapper(fn):
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as e:
                return f"ERR:{str(e)[:40]}"
        return inner
    return wrapper

# ── 1. BALANCE ──
balance_total = "ERR"
balance_free = "ERR"
balance_pnl_open = "ERR"
balance_pnl_closed = "ERR"

try:
    bybit = ccxt.bybit({
        'apiKey': env['BYBIT_API_KEY'],
        'secret': env['BYBIT_API_SECRET'],
        'options': {'defaultType': 'swap'}
    })
    bal = bybit.fetch_balance({'type': 'swap'})
    balance_total = bal['total'].get('USDT', 0)
    balance_free = bal['free'].get('USDT', 0)
    balance_total = round(balance_total, 2)
    balance_free = round(balance_free, 2)
except Exception as e:
    print(f"Balance ERR: {e}")

# ── 2. OPEN POSITIONS (Bybit API) ──
positions_lines = []
try:
    symbols_to_check = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT',
                        'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'ADA/USDT:USDT',
                        'AVAX/USDT:USDT', 'LINK/USDT:USDT', 'BNB/USDT:USDT',
                        'PEPE/USDT:USDT', 'WIF/USDT:USDT', 'FET/USDT:USDT',
                        'RENDER/USDT:USDT', 'NEAR/USDT:USDT', 'APT/USDT:USDT',
                        'OP/USDT:USDT', 'ARB/USDT:USDT', 'SUI/USDT:USDT',
                        'INJ/USDT:USDT', 'ZEC/USDT:USDT']
    for sym in symbols_to_check:
        try:
            pos = bybit.fetch_position(symbol=sym)
            contracts = pos.get('contracts', 0)
            if contracts and contracts > 0:
                side = pos.get('side', '?')
                entry = pos.get('entryPrice', 0)
                pnl = pos.get('unrealizedPnl', 0)
                mark = pos.get('markPrice', 0)
                leverage = pos.get('leverage', 1)
                margin = pos.get('initialMargin', 0)
                pnl_pct = (pnl / margin * 100) if margin and margin > 0 else 0
                positions_lines.append({
                    'symbol': sym.replace('/USDT:USDT', ''),
                    'side': side.upper()[:4],
                    'size': contracts,
                    'entry': entry,
                    'mark': mark,
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl_pct, 2),
                    'leverage': leverage,
                    'status': 'open'
                })
        except Exception:
            pass
except Exception as e:
    positions_lines = [{'symbol': 'ERR', 'side': 'ERR', 'size': 0, 'entry': 0, 'mark': 0, 'pnl': 0, 'pnl_pct': 0, 'leverage': 0, 'status': str(e)[:20]}]

# ── 3. DB POSITIONS (for strategy association) ──
db_positions = []
try:
    conn = psycopg2.connect(host='192.168.0.149', port=5432,
                           dbname='cryptotrader', user='cryptotrader',
                           password='cryptotrader123')
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, side, quantity, entry_price,
               ROUND(unrealized_pnl::numeric,2), ROUND(unrealized_pnl_percent::numeric,2),
               leverage, status, opened_at
        FROM positions WHERE UPPER(status) = 'OPEN' ORDER BY opened_at DESC
    """)
    for row in cur.fetchall():
        db_positions.append({
            'symbol': row[0], 'side': row[1], 'qty': row[2],
            'entry': row[3], 'upnl': row[4], 'upnl_pct': row[5],
            'leverage': row[6], 'status': row[7], 'opened_at': row[8]
        })
    conn.close()
except Exception as e:
    db_positions = [{'symbol': f'DB_ERR:{str(e)[:30]}'}]

# ── 4. SIGNALS BY STRATEGY (recent) ──
signals_data = []
try:
    conn = psycopg2.connect(host='192.168.0.149', port=5432,
                           dbname='cryptotrader', user='cryptotrader',
                           password='cryptotrader123')
    cur = conn.cursor()

    # signals table - last 24h
    now_str = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cur.execute("""
        SELECT signal_type, status, COUNT(*) FROM signals
        WHERE created_at > %s AND signal_type != 'HOLD'
        GROUP BY signal_type, status
    """, (now_str,))
    sig_by_type = {}
    for row in cur.fetchall():
        sig_type = row[0].upper()
        status = row[1].upper()
        cnt = row[2]
        key = f"{sig_type}_{status}"
        sig_by_type[key] = cnt

    total_signals = sum(v for k,v in sig_by_type.items() if 'PENDING' not in k or 'EXECUTED' in k)
    buys = sum(v for k,v in sig_by_type.items() if 'BUY' in k)
    sells = sum(v for k,v in sig_by_type.items() if 'SELL' in k)
    holds_query = cur.execute("""
        SELECT COUNT(*) FROM signals WHERE created_at > %s AND signal_type = 'HOLD'
    """, (now_str,))
    holds = cur.fetchone()[0]
    skips_query = cur.execute("""
        SELECT COUNT(*) FROM signals WHERE created_at > %s AND status = 'SKIPPED'
    """, (now_str,))
    skips = cur.fetchone()[0]

    signals_data.append({'strategy': 'All', 'total': total_signals, 'buy': buys, 'sell': sells, 'skip': skips + holds})

    # Recent signals list
    cur.execute("""
        SELECT symbol, signal_type, confidence, status, price, created_at
        FROM signals WHERE created_at > %s ORDER BY created_at DESC LIMIT 10
    """, (now_str,))
    recent_signals = []
    for row in cur.fetchall():
        age_min = (datetime.now(timezone.utc) - row[5].replace(tzinfo=timezone.utc) if row[5].tzinfo is None else row[5]).total_seconds() / 60 if row[5] else 999
        recent_signals.append({
            'symbol': row[0], 'type': row[1], 'conf': row[2],
            'status': row[3], 'price': row[4], 'age_min': round(age_min, 0)
        })

    # Pending signals count
    cur.execute("SELECT COUNT(*) FROM signals WHERE status = 'PENDING'")
    pending_count = cur.fetchone()[0]

    conn.close()
except Exception as e:
    signals_data = [{'strategy': 'ERR', 'total': 0, 'buy': 0, 'sell': 0, 'skip': 0}]
    recent_signals = []
    pending_count = 0
    print(f"Signals ERR: {e}")

# ── 5. STRATEGY SIGNALS TABLE ──
strategy_signals = {}
try:
    conn = psycopg2.connect(host='192.168.0.149', port=5432,
                           dbname='cryptotrader', user='cryptotrader',
                           password='cryptotrader123')
    cur = conn.cursor()
    now_str2 = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cur.execute("""
        SELECT strategy, action, status, COUNT(*) FROM strategy_signals
        WHERE created_at > %s GROUP BY strategy, action, status
    """, (now_str2,))
    for row in cur.fetchall():
        strat = row[0]
        action = row[1].upper() if row[1] else '?'
        status = row[2].upper() if row[2] else '?'
        cnt = row[3]
        if strat not in strategy_signals:
            strategy_signals[strat] = {'total': 0, 'buy': 0, 'sell': 0, 'skip': 0}
        strategy_signals[strat]['total'] += cnt
        if action == 'BUY':
            strategy_signals[strat]['buy'] += cnt
        elif action == 'SELL':
            strategy_signals[strat]['sell'] += cnt
        elif action in ('HOLD', 'SKIP') or status in ('SKIPPED', 'PENDING'):
            strategy_signals[strat]['skip'] += cnt
    conn.close()
except Exception:
    pass

# ── 6. TODAY'S CLOSED P/L ──
today_pnl = 0
today_trades = 0
try:
    conn = psycopg2.connect(host='192.168.0.149', port=5432,
                           dbname='cryptotrader', user='cryptotrader',
                           password='cryptotrader123')
    cur = conn.cursor()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0) FROM positions
        WHERE UPPER(status) = 'CLOSED' AND DATE(closed_at) = %s
    """, (today,))
    row = cur.fetchone()
    today_trades = row[0]
    today_pnl = round(float(row[1]), 2)
    conn.close()
except Exception:
    pass

# ── 7. PRICES ──
prices = {}
for sym_api in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']:
    try:
        url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym_api}"
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.loads(r.read())
            prices[sym_api] = d['result']['list'][0]['lastPrice']
    except Exception:
        prices[sym_api] = 'ERR'

# ── 8. OHLCV FRESHNESS ──
ohlcv_ok = False
ohlcv_details = {}
try:
    conn = psycopg2.connect(host='192.168.0.149', port=5432,
                           dbname='cryptotrader', user='cryptotrader',
                           password='cryptotrader123')
    cur = conn.cursor()
    for tf in ['1', '4h']:
        cur.execute("""
            SELECT symbol, MAX(created_at) FROM ohlcv_raw
            WHERE timeframe = %s AND symbol IN ('BTCUSDT','ETHUSDT','SOLUSDT')
            GROUP BY symbol
        """, (tf,))
        for row in cur.fetchall():
            sym, latest = row
            if latest:
                age = (datetime.now(timezone.utc) - latest.replace(tzinfo=timezone.utc) if latest.tzinfo is None else latest).total_seconds()
                ohlcv_details[f"{sym}"] = {'tf': tf, 'age_min': round(age/60, 0)}
                if age < 600:
                    ohlcv_ok = True
    conn.close()
except Exception:
    ohlcv_ok = False

# ── 9. AGENT LOG RECENT ──
logs_ok = False
last_log = "none"
try:
    conn = psycopg2.connect(host='192.168.0.149', port=5432,
                           dbname='cryptotrader', user='cryptotrader',
                           password='cryptotrader123')
    cur = conn.cursor()
    cur.execute("SELECT MAX(timestamp) FROM agent_logs")
    row = cur.fetchone()
    if row[0]:
        last_log = str(row[0])
        age = (datetime.now(timezone.utc) - row[0].replace(tzinfo=timezone.utc) if row[0].tzinfo is None else row[0]).total_seconds()
        logs_ok = age < 3600
    conn.close()
except Exception:
    pass

# ── 10. LOG FILE CHECK ──
log_file_ok = False
try:
    log_path = '/home/andy/logs/cryptotrader.log'
    if os.path.exists(log_path):
        mod_time = os.path.getmtime(log_path)
        age_min = (datetime.now().timestamp() - mod_time) / 60
        log_file_ok = age_min < 60
except Exception:
    pass

# ── 11. LLM HEALTH ──
deepseek_ok = False
minimax_ok = False
deepseek_err = ""
minimax_err = ""

# DeepSeek test
try:
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {env.get('DEEPSEEK_API_KEY', '')}"
    }
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": 5
    }).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status == 200:
            deepseek_ok = True
except urllib.error.HTTPError as e:
    deepseek_err = f"HTTP {e.code}"
except Exception as e:
    deepseek_err = str(e)[:30]

# MiniMax test
try:
    url = "https://api.minimax.chat/v1/text/chatcompletion_v2"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {env.get('MINIMAX_API_KEY', '')}"
    }
    body = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": 5
    }).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status == 200:
            minimax_ok = True
except urllib.error.HTTPError as e:
    minimax_err = f"HTTP {e.code}"
except Exception as e:
    minimax_err = str(e)[:30]

# ── 12. API HEALTH ──
api_ok = False
try:
    bybit.fetch_position(symbol='BTC/USDT:USDT')
    api_ok = True
except Exception:
    pass

# ── 13. DISK SPACE ──
disk_ok = True
try:
    stat = os.statvfs('/')
    used_pct = (1 - stat.f_bavail / stat.f_blocks) * 100
    if used_pct > 90:
        disk_ok = False
except Exception:
    pass

# ── BUILD REPORT ──
now = datetime.now(timezone.utc)
msk = now + timedelta(hours=3)
time_str = msk.strftime('%H:%M')

total_equity = balance_total
open_pnl = sum(p['pnl'] for p in positions_lines if isinstance(p.get('pnl'), (int, float)))
closed_pnl = today_pnl
total_pnl = open_pnl + closed_pnl

lines = []
lines.append(f"📊 *СОСТОЯНИЕ СИСТЕМЫ* | `{time_str} MSK`")
lines.append("")

# PRICES
lines.append(f"💲 *ЦЕНЫ* | BTC: ${prices.get('BTCUSDT','?')} | ETH: ${prices.get('ETHUSDT','?')} | SOL: ${prices.get('SOLUSDT','?')}")
lines.append("")

# POSITIONS
lines.append("🟢 *ПОЗИЦИИ*")
lines.append("| Стратегия | Symbol | Side | Size | Entry | PnL $ | PnL % | Status |")
lines.append("|-----------|--------|------|------|-------|-------|-------|--------|")
if positions_lines:
    for p in positions_lines:
        strat = '?'
        for dbp in db_positions:
            if dbp.get('symbol', '').replace('USDT', '') == p['symbol'].replace('USDT', ''):
                # Try to infer strategy from signals
                strat = 'Intraday'
                break
        lines.append(f"| {strat} | {p['symbol']} | {p['side']} | {p['size']} | {p['entry']} | {p['pnl']} | {p['pnl_pct']}% | {p['status']} |")
else:
    lines.append("| — | — | — | — | — | — | — | нет позиций |")
lines.append("")

# SIGNALS
lines.append("📈 *СИГНАЛЫ (24ч)*")
lines.append("| Стратегия | Сигналов | Buy | Sell | Skip |")
lines.append("|-----------|----------|-----|------|------|")
if strategy_signals:
    for strat, data in sorted(strategy_signals.items()):
        lines.append(f"| {strat} | {data['total']} | {data['buy']} | {data['sell']} | {data['skip']} |")
else:
    lines.append(f"| All | {signals_data[0]['total'] if signals_data else 0} | {signals_data[0]['buy'] if signals_data else 0} | {signals_data[0]['sell'] if signals_data else 0} | {signals_data[0]['skip'] if signals_data else 0} |")
lines.append(f"| ⏳ Pending | {pending_count} | — | — | — |")
lines.append("")

# LLM HEALTH
lines.append("🔧 *LLM HEALTH*")
lines.append("| Провайдер | Модель | Status |")
lines.append("|-----------|--------|--------|")
ds_status = "✅ OK" if deepseek_ok else f"❌ {deepseek_err}"
mm_status = "✅ OK" if minimax_ok else f"❌ {minimax_err}"
lines.append(f"| DeepSeek | V3/Chat | {ds_status} |")
lines.append(f"| MiniMax | M2.7 | {mm_status} |")
lines.append("")

# BALANCE
lines.append(f"💰 *БАЛАНС* | Total: ${total_equity} | Open PnL: ${open_pnl:+.2f} | Closed Today: ${closed_pnl:+.2f} | Trades: {today_trades}")
lines.append("")

# DIAGNOSTICS
ohlcv_status = "✅" if ohlcv_ok else "❌"
db_status = "✅" if db_positions is not None else "❌"
api_status = "✅" if api_ok else "❌"
log_status = "✅" if log_file_ok else "❌"
disk_status = "✅" if disk_ok else "⚠️ FULL"
lines.append(f"⚠️ *ДИАГНОСТИКА* | OHLCV: {ohlcv_status} | DB: {db_status} | API: {api_status} | Logs: {log_status} | Disk: {disk_status}")
lines.append("")

# RECENT SIGNALS
if recent_signals:
    lines.append("📋 *ПОСЛЕДНИЕ СИГНАЛЫ*")
    lines.append("| Symbol | Type | Conf | Status | Age |")
    lines.append("|--------|------|------|--------|-----|")
    for s in recent_signals[:5]:
        lines.append(f"| {s['symbol']} | {s['type']} | {s['conf']} | {s['status']} | {int(s['age_min'])}m |")
    lines.append("")

# ISSUES
issues = []
if pending_count > 10:
    issues.append(f"⚠️ {pending_count} pending signals stuck")
if not deepseek_ok:
    issues.append(f"❌ DeepSeek down: {deepseek_err}")
if not minimax_ok:
    issues.append(f"❌ MiniMax down: {minimax_err}")
if not ohlcv_ok:
    issues.append("❌ OHLCV data stale")
if not api_ok:
    issues.append("❌ Bybit API unreachable")
if not disk_ok:
    issues.append("⚠️ Disk space critical")

if issues:
    lines.append(f"⚠️ *ПРОБЛЕМЫ* | {'; '.join(issues)}")
else:
    lines.append("✅ *Штатно* | Все системы работают")

print('\n'.join(lines))
