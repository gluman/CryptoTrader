#!/usr/bin/env python3
"""
Единый источник DSN-параметров для подключения к PostgreSQL.

R13 FIX (code review iter.3): убран дубль хардкода host="192.168.0.149"
из 5+ файлов (clone5_multi_runner, multi_strategy_monitor, compound_dashboard,
oos_live_validator, strategy_health_check). Теперь все читают отсюда.

Использование:
    from cryptotrader_strategies.db_safe import db_dsn
    dsn = db_dsn()  # dict(host, port, database, user, password)

ЗАЧЕМ: один source of truth → смена IP/порта/БД = правка в одном месте.
СМ. ТАКЖЕ: bybit_safe.py (аналог для Bybit exchange connection).
"""
from __future__ import annotations

import os


def db_dsn() -> dict:
    """Вернуть dict с параметрами подключения к PostgreSQL.

    Приоритет: переменные окружения > дефолты.
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_PASSWORD — из .env (dotenv).
    """
    return dict(
        host=os.environ.get("POSTGRES_HOST", "192.168.0.149"),
        port=int(os.environ.get("POSTGRES_PORT", 5432)),
        database="cryptotrader",
        user="cryptotrader",
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )
