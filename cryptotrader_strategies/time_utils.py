"""
Timezone helpers — единый модуль для отчётов CryptoTrader.

Все cron-скрипты и мониторы выводят время в Europe/Moscow (UTC+3).
Чтобы не разъезжались часовые пояса между агентом и Боссом, используем
одну функцию: `now_msk_str()` / `to_msk_str(iso)`.

WHY: Босс читает отчёты в Москве; cron пишет timestamp'ы локально (UTC
или naive), ccxt отдаёт UTC. Единая утилита избегает путаницы и
скрытой ошибки "выглядит нормально, но часовой пояс не тот".

WHAT: datetime.now(TZ) → ISO string в MSK; форматирование %d.%m.%Y %H:%M MSK.

USAGE:
    from time_utils import now_msk_str
    print(f"[{now_msk_str()}] v7a: HOLD")  # [08.06.2026 22:21 MSK] v7a: HOLD

DEPLOY:
    Скопировано в /home/andy/.hermes/scripts/time_utils.py.
    В каждом cron-скрипте CryptoTrader — `sys.path.insert(0, '/home/andy/.hermes/scripts')`
    + `from time_utils import now_msk_str`.

SEE ALSO:
    cron/bybit_quota_check.py
    cron/strategy_health_check.py
    multi_strategy_monitor.py
    execute_cron.py
    oos_live_validator.py
"""
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))  # Europe/Moscow (UTC+3, no DST since 2014)


def now_msk_str() -> str:
    """Текущее время в MSK, формат '08.06.2026 22:21 MSK'."""
    return datetime.now(MSK).strftime("%d.%m.%Y %H:%M MSK")


def to_msk_str(dt) -> str:
    """Любой datetime (naive/aware) → MSK string '08.06.2026 22:21 MSK'.

    Naive datetime интерпретируется как UTC (для OHLCV ccxt, db timestamps).
    """
    if dt is None:
        return "n/a"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M MSK")


def msk_iso_now() -> str:
    """ISO 8601 в MSK — для JSON output, машинно-читаемый."""
    return datetime.now(MSK).isoformat(timespec="seconds")
