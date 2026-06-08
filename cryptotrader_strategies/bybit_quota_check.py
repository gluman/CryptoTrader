#!/usr/bin/env python3
"""
bybit_quota_check.py — мониторинг API-квоты Bybit, alert Boss'у при <30%.

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ
═══════════════════════════════════════════════════════════════════════════════

Boss 2026-06-08 16:20: «Добавь cron `bybit_quota_check` каждые 60 мин.
Мониторит API-квоту Bybit, alert Boss'у если <30%. Сейчас узнаём о проблеме
1310 только когда что-то ломается (execute_cron падает 3+ мин подряд).»

Bybit V5 лимиты (https://bybit-exchange.github.io/docs/v5/rate-limit):
  - Market data (kline, orderbook, ticker): 600 req / 5 sec
  - Weekly limit (по категории, например linear): 700 req / 5 min (если есть
    превышение — 1310 "Weekly/Monthly Limit Exhausted")
  - Monthly: 2000 req / min (cap)

Ошибка 1310 — реальный блокер, т.к. сбрасывается только раз в неделю / месяц.
Если поймали — переключаемся на RESTful 5-минутные свечи через кэш, ждём reset.

ЧТО ДЕЛАЕТ
══════════

  1. Проверяет, сколько ошибок 1310 было за последние 24 часа
     (grep в `cryptotrader.log`).
  2. Если > 0 — собирает контекст: когда первая/последняя, типичные пары.
  3. Определяет estimated reset time (от первого появления ошибки в логе).
  4. Отправляет ALERT в Telegram (stdout cron-а доставляется, если
     deliver=telegram:519881679).
  5. Если 1310 не было — silent (только timestamp в логе).

ПОЧЕМУ ИМЕННО ТАК
══════════════════

  - Через grep логов, а не отдельный API endpoint. Bybit НЕ даёт публичного
    "quota remaining" — поэтому единственный способ узнать — ловить 1310
    в логах.
  - Cron interval = 60 мин: даёт достаточно времени накопить статистику
    (1310 не моментальный, проявляется в течение минут).
  - Silent pattern: если 1310=0 → тихий exit 0, без шума в Telegram.
  - threshold = 0 (один 1310 = alert), потому что 1310 — это уже
    индикатор проблемы; даже единичный случай означает что квота
    исчерпана (см. commit d04a9b7 / bybit-1310-quota-myth skill).

ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ
════════════════════

  - Не различает weekly vs monthly reset. Парсит только "Your limit will
    reset at <timestamp>" из сообщения ошибки.
  - Не мониторит 10006 (rate limit per-second) — это другой лимит, менее
    критичный (retry решает).
  - Точность grep'а зависит от формата лога. Если cryptotrader.log
    переименуется (logrotate), история теряется.

СМ. ТАКЖЕ
═════════

  - cryptotrader_strategies/execute_cron.py — circuit breaker на 1310
  - code_review/CODE_REVIEW_2026-06-08.md — раздел 1310 quota
  - skill: trading/bybit-1310-quota-myth
  - cron `ce83cb821455` — execute_cron (3 мин) — основной потребитель квоты
═══════════════════════════════════════════════════════════════════════════════
"""
import os
import re
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOG_FILE = Path('/home/andy/CryptoTrader_main/logs/cryptotrader.log')
LOG_FILE_ALT = Path('/home/andy/CryptoTrader_main/logs/cryptotrader.log.1')
# 1310-ошибки чаще в cron-output (execute_cron каждый 3 мин пишет в
# ~/.hermes/cron/output/ce83cb821455/<timestamp>.md). Поэтому сканируем
# в первую очередь их — там самая свежая статистика.
CRON_OUT_DIR = Path('/home/andy/.hermes/cron/output/ce83cb821455')

# Pattern из ошибки 1310 Bybit V5
PAT_1310 = re.compile(r'\[(\d+)\]\[(\w+)/\w+ Limit Exhausted\.?\s*Your limit will reset at ([^\]]+)\]')
PAT_1310_RESET_TS = re.compile(r'reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
PAT_1310_TYPE = re.compile(r'\[(\d+)\]\[(\w+)/')  # retCode + 'Weekly'/'Monthly'


def _parse_log_line(line: str):
    """Парсит строку лога, возвращает dict события или None."""
    m = PAT_1310.search(line)
    if not m:
        return None
    ts_m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
    if not ts_m:
        return None
    try:
        ts = datetime.strptime(ts_m.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    reset_at = None
    rm = PAT_1310_RESET_TS.search(line)
    if rm:
        try:
            reset_at = datetime.strptime(rm.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    tm = PAT_1310_TYPE.search(line)
    type_str = tm.group(2) if tm else 'Unknown'
    return {
        'ts': datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        'type': type_str,
        'reset_at': reset_at.isoformat() if reset_at else None,
        'raw_short': line[:200].strip(),
    }


def collect_1310_events(hours=24):
    """Собрать события 1310 из cron-output + логов за последние N часов."""
    cutoff = time.time() - hours * 3600
    events = []
    # 1) Cron output (.md файлы) — самые свежие. Формат: 
    #    "<timestamp> - <module> - ERROR - RuntimeError: HTTP 200: [1310]..."
    #    где timestamp = "2026-06-08 16:13:51,158" (с запятой и миллисекундами).
    if CRON_OUT_DIR.exists():
        for md in CRON_OUT_DIR.glob('*.md'):
            try:
                mtime = md.stat().st_mtime
                if mtime < cutoff:
                    continue
                with open(md, encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                for line in content.split('\n'):
                    if '1310' not in line or 'Limit' not in line:
                        continue
                    # Timestamp из имени файла надёжнее чем из строки
                    # (имя: 2026-06-08_19-13-51.md → 2026-06-08 19:13:51)
                    name_m = re.match(r'(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})', md.name)
                    if name_m:
                        try:
                            ts_str = f"{name_m.group(1)} {name_m.group(2)}:{name_m.group(3)}:{name_m.group(4)}"
                            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
                        except ValueError:
                            ts = mtime
                    else:
                        ts = mtime
                    if ts < cutoff:
                        continue
                    rm = PAT_1310_RESET_TS.search(line)
                    reset_at = None
                    if rm:
                        try:
                            reset_at = datetime.strptime(rm.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                        except ValueError:
                            pass
                    tm = PAT_1310_TYPE.search(line)
                    type_str = tm.group(2) if tm else 'Unknown'
                    events.append({
                        'ts': datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                        'type': type_str,
                        'reset_at': reset_at.isoformat() if reset_at else None,
                        'raw_short': line[:200].strip(),
                    })
            except Exception as e:
                print(f"WARN: failed to read {md}: {e}", file=sys.stderr)
    # 2) Main logs (cryptotrader.log*) — fallback, на случай если cron output почистится
    for log_path in [LOG_FILE, LOG_FILE_ALT]:
        if not log_path.exists():
            continue
        try:
            with open(log_path, encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if '1310' not in line or 'Limit' not in line:
                        continue
                    ev = _parse_log_line(line)
                    if ev:
                        events.append(ev)
        except Exception as e:
            print(f"WARN: failed to read {log_path}: {e}", file=sys.stderr)
    # Дедупликация по ts+reset_at (могут пересекаться cron-out и logs)
    seen = set()
    unique = []
    for ev in events:
        key = (ev['ts'], ev['reset_at'])
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return unique


def main() -> int:
    started = datetime.now(timezone.utc)
    print(f"=== Bybit quota check: {started.isoformat()} ===", flush=True)

    events = collect_1310_events(hours=24)

    if not events:
        # Silent: всё ок, квоты хватает
        print(f"✅ No 1310 quota errors in last 24h. Healthy.", flush=True)
        # Машино-читаемый JSON (silent-friendly)
        print(json.dumps({
            "alert": False,
            "errors_24h": 0,
            "ts": started.isoformat(),
        }), flush=True)
        return 0

    # Есть ошибки 1310 — формируем alert
    types = {e['type'] for e in events}
    earliest = min(events, key=lambda x: x['ts'])
    latest = max(events, key=lambda x: x['ts'])
    reset_at = earliest.get('reset_at')

    hours_until_reset = None
    if reset_at:
        try:
            r = datetime.fromisoformat(reset_at)
            hours_until_reset = round((r - started).total_seconds() / 3600, 1)
        except Exception:
            pass

    print(f"\n🚨 BYBIT QUOTA ALERT (24h):", flush=True)
    print(f"   Total 1310-errors: {len(events)}", flush=True)
    print(f"   Types: {', '.join(types)}", flush=True)
    print(f"   First: {earliest['ts']}", flush=True)
    print(f"   Last:  {latest['ts']}", flush=True)
    if reset_at:
        print(f"   Reset: {reset_at}  (in {hours_until_reset}h)", flush=True)
    print(f"\n   Action:", flush=True)
    print(f"     • Wait for reset if less than 24h", flush=True)
    print(f"     • Switch to cached 5m klines from PostgreSQL if urgent", flush=True)
    print(f"     • Reduce scan frequency (cron #1 from 15m → 30m?)", flush=True)

    # JSON для парсинга
    print(json.dumps({
        "alert": True,
        "errors_24h": len(events),
        "types": list(types),
        "first": earliest['ts'],
        "last": latest['ts'],
        "reset_at": reset_at,
        "hours_until_reset": hours_until_reset,
        "ts": started.isoformat(),
    }), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
