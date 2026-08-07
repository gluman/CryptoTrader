#!/usr/bin/env python3
"""
strategy_health_check.py — проверяет, что production-стратегии живы.

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ
═══════════════════════════════════════════════════════════════════════════════

Boss 2026-06-08 16:25: «Добавь cron `strategy_health_check` каждые 4 часа.
Проверяет, что v7a выдаёт ≥ 1 сигнал за неделю. Alert если 0. 18 дней без
сделок прошли незамеченными.»

Проблема: v7a в production с 21.05.2026 — за 18 дней ни одной новой позиции.
Никто не алёртил. Если стратегия реально сломалась (баг, market regime
change, OHLCV corruption) — мы узнаём через недели.

ЧТО ДЕЛАЕТ
══════════

  1. Подключается к PostgreSQL `192.168.0.149:5432/cryptotrader`.
  2. Считает:
     - signals_24h, signals_7d по каждой production-стратегии
     - closed_positions_24h, closed_positions_7d
     - last_signal_ts (timestamp последнего сигнала)
     - last_trade_ts (timestamp последней закрытой позиции)
  3. Сравнивает с thresholds:
     - alerts if signals_7d = 0 для любой production-стратегии
     - alerts if last_signal_ts > 14 days ago (долго молчит)
     - alerts если OHLCV-данные stale > 30 min для must_haves пар
  4. Отправляет ALERT в Telegram (cron deliver=telegram:519881679).
  5. Если всё ок — silent (только timestamp + JSON с health).

ПОЧЕМУ ИМЕННО ТАК
══════════════════

  - Через PostgreSQL, а не через Bybit API. Сигналы и сделки в БД —
    единственный надёжный источник (Bybit не даёт историю signals).
  - Hard-coded thresholds в коде: 0 signals/7d = плохо, 14d молчание = критично.
  - Только production-стратегии: список берём из
    config.settings.yaml `trading_decision.symbols` (8 пар Clone5 v7).
  - Silent pattern: cron no_agent=True + deliver=telegram — если пустой
    stdout, то ничего не отправится.

ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ
════════════════════

  - Не проверяет качество сигналов (PF, WR) — это OOS validator делает.
  - Считает ТОЛЬКО signals и closed positions. Не считает pending (в
    работе). Если все сигналы pending и не исполнились — alert будет
    ложный (но редкий случай).
  - OHLCV staleness проверяется по тем же must_haves, что и DataCollector.
    Если DataCollector отключён — все 9 пар stale = mass alert.

СМ. ТАКЖЕ
═════════

  - cryptotrader_strategies/oos_live_validator.py — quality (PF/WR)
  - cryptotrader_strategies/bybit_quota_check.py — quota errors
  - skill: trading/bybit-1310-quota-myth — почему "OHLCV stale ≠ 1310"
═══════════════════════════════════════════════════════════════════════════════
"""
import os
import sys
import json
import time
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Единый timezone helper (MSK UTC+3) — рядом лежит в /home/andy/.hermes/scripts
sys.path.insert(0, '/home/andy/.hermes/scripts')
from time_utils import now_msk_str, msk_iso_now, to_msk_str  # noqa: E402

# PGPASSFILE workaround (execute_code маскирует PGPASSWORD)
PGPASS = '/tmp/.pgpass_ct'

# Production-стратегии (должны быть активны)
PROD_STRATEGIES = ['clone5_v7_trailing_only']
# OHLCV must_haves (8 пар Clone5 v7, из config/settings.yaml)
MUST_HAVE_PAIRS = [
    'XRPUSDT', 'DOGEUSDT', 'TONUSDT', 'SUIUSDT', 'NEARUSDT',
    'SOLUSDT', 'LITUSDT', 'WLDUSDT', 'ADAUSDT',
]
OHLCV_STALE_MIN = 30  # минут — alert если свечи старше


def _psql_query(sql: str) -> str:
    """Запуск psql с PGPASSFILE workaround."""
    env = os.environ.copy()
    env['PGPASSFILE'] = PGPASS
    r = subprocess.run(
        ['psql', '-h', '192.168.0.149', '-U', 'cryptotrader',
         '-d', 'cryptotrader', '-t', '-A', '-c', sql],
        capture_output=True, text=True, timeout=30, env=env,
    )
    return r.stdout.strip()


def get_strategy_signals(days: int = 7):
    """Сигналы по каждой стратегии за N дней."""
    sql = f"""
        SELECT strategy,
               COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours') AS sigs_24h,
               COUNT(*) AS sigs_7d,
               MAX(created_at) AS last_ts
        FROM strategy_signals
        WHERE created_at > NOW() - INTERVAL '{days} days'
          AND strategy IN ({','.join(f"'{s}'" for s in PROD_STRATEGIES)})
        GROUP BY strategy
        ORDER BY strategy;
    """
    out = _psql_query(sql)
    result = []
    for line in out.split('\n'):
        if not line or '|' not in line:
            continue
        parts = line.split('|')
        if len(parts) < 4:
            continue
        result.append({
            'strategy': parts[0],
            'sigs_24h': int(parts[1] or 0),
            'sigs_7d': int(parts[2] or 0),
            'last_ts': parts[3] or None,
        })
    return result


def get_closed_positions(days: int = 7):
    """Закрытые позиции по стратегиям за N дней (по notes LIKE)."""
    result = {}
    for strat in PROD_STRATEGIES:
        sql = f"""
            SELECT COUNT(*), MAX(closed_at)
            FROM positions
            WHERE upper(status)='CLOSED'
              AND notes LIKE '%{strat}%'
              AND closed_at > NOW() - INTERVAL '{days} days';
        """
        out = _psql_query(sql)
        if out and '|' in out:
            parts = out.split('|')
            result[strat] = {
                'closed_7d': int(parts[0] or 0),
                'last_close': parts[1] or None,
            }
    return result


def get_ohlcv_freshness():
    """Возраст последнего 5m бара по must_haves парам."""
    pairs_sql = ','.join(f"'{p}'" for p in MUST_HAVE_PAIRS)
    sql = f"""
        SELECT symbol,
               EXTRACT(EPOCH FROM (NOW() - MAX(timestamp)))::int/60 AS age_min
        FROM ohlcv_raw
        WHERE exchange='bybit' AND timeframe='5m' AND symbol IN ({pairs_sql})
        GROUP BY symbol
        ORDER BY age_min DESC NULLS FIRST;
    """
    out = _psql_query(sql)
    result = []
    for line in out.split('\n'):
        if not line or '|' not in line:
            continue
        parts = line.split('|')
        if len(parts) < 2:
            continue
        try:
            age = int(parts[1]) if parts[1] else None
        except ValueError:
            age = None
        result.append({'symbol': parts[0], 'age_min': age})
    return result


def _fmt_optional(raw: str | None) -> str:
    """
    Конвертирует psql naive ISO-строку в MSK для Telegram.

    Без timezone — потому что PostgreSQL `created_at` хранится в UTC (TIMESTAMPTZ),
    но psycopg2 + `psql -At` отдаёт naive в большинстве случаев.

    Returns "—" для None/empty, "raw" если формат неожиданный.
    """
    if not raw:
        return "—"
    # Не парсим микросекунды + tz — относимся к строке как UTC.
    try:
        # Типичный формат: "2026-06-08 17:00:40.209327+00" или
        # "2026-06-08 17:00:40.209327+00:00"
        s = raw.strip().replace(" ", "T")
        # Унифицируем "+00" → "+00:00" чтобы fromisoformat работал.
        if s.endswith("+00"):
            s = s[:-3] + "+00:00"
        dt = datetime.fromisoformat(s)
        return to_msk_str(dt)
    except Exception:
        return raw  # fallback — отдаём как есть


def main() -> int:
    started = datetime.now(timezone.utc)
    print(f"=== Strategy Health Check: {now_msk_str()} ===", flush=True)

    sigs = get_strategy_signals(days=7)
    closed = get_closed_positions(days=7)
    freshness = get_ohlcv_freshness()

    # Анализ
    alerts = []
    healthy = True

    for s in sigs:
        strat = s['strategy']
        if s['sigs_7d'] == 0:
            alerts.append(f"{strat}: 0 signals за 7 дней (last: {s['last_ts'] or 'НИКОГДА'})")
            healthy = False
        else:
            # psql отдаёт naive datetime как "2026-06-08 17:00:40.209327+00" (UTC).
            # Конвертируем в MSK для читабельности в Telegram.
            last_msk = _fmt_optional(s['last_ts'])
            print(f"  {strat:30} sigs_24h={s['sigs_24h']:3}  sigs_7d={s['sigs_7d']:3}  last={last_msk}", flush=True)

    for strat, c in closed.items():
        if c['closed_7d'] == 0:
            # Не алертим — может быть ранняя стадия стратегии
            print(f"  {strat:30} closed_7d=0  (нет закрытых сделок)", flush=True)
        else:
            last_close_msk = _fmt_optional(c['last_close'])
            print(f"  {strat:30} closed_7d={c['closed_7d']}  last_close={last_close_msk}", flush=True)

    # OHLCV freshness
    print("", flush=True)
    print("OHLCV freshness (5m, bybit):", flush=True)
    stale_count = 0
    for f in freshness:
        age = f['age_min']
        marker = "✓" if age is not None and age < OHLCV_STALE_MIN else "✗"
        if age is None or age >= OHLCV_STALE_MIN:
            stale_count += 1
        print(f"  {marker} {f['symbol']:10}  age={age} min" if age is not None else f"  ✗ {f['symbol']:10}  NO DATA", flush=True)
    if stale_count > 0:
        alerts.append(f"{stale_count}/{len(freshness)} must_have пар stale > {OHLCV_STALE_MIN}m (DataCollector?)")
        healthy = False

    # JSON summary
    print("", flush=True)
    if healthy:
        print(f"✅ All {len(PROD_STRATEGIES)} production strategies healthy.", flush=True)
        print(json.dumps({
            "alert": False,
            "strategies": sigs,
            "closed": closed,
            "ohlcv_stale_count": stale_count,
            "ts": msk_iso_now(),
            "ts_msk": now_msk_str(),
        }), file=sys.stderr, flush=True)
        return 0

    # ALERT mode
    print(f"\n🚨 STRATEGY HEALTH ALERT:", flush=True)
    for a in alerts:
        print(f"   • {a}", flush=True)
    print(json.dumps({
        "alert": True,
        "alerts": alerts,
        "strategies": sigs,
        "closed": closed,
        "ohlcv_stale_count": stale_count,
        "ts": msk_iso_now(),
        "ts_msk": now_msk_str(),
    }), file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
