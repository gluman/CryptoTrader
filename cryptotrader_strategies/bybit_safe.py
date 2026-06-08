"""
Bybit safe access helpers — единая обёртка для ccxt-инициализации с time-sync.

Все скрипты (compound_engine, multi_strategy_monitor, compound_dashboard, Clone5 runner)
должны импортировать bybit_exchange() вместо собственного ccxt.bybit({...}).

Зачем: код-ревью 2026-06-08 замечания H1/H5/H6.
- Без `adjustForTimeDifference=True` + `load_time_difference()` подписанные запросы
  падают с `retCode 10002` (timestamp mismatch).
- Без `options.recvWindow` Bybit режет по умолчанию 5000 ms.
- `recvWindow` верхним уровнем конструктора ccxt игнорирует (нужно внутрь options).
"""
from __future__ import annotations

import os
import logging
from typing import Optional

import ccxt

log = logging.getLogger(__name__)

_DEFAULTS = {
    "enableRateLimit": True,
    "options": {
        "defaultType": "linear",
        "adjustForTimeDifference": True,
        "recvWindow": 60000,
    },
    "timeout": 10000,
}


def _require_env() -> tuple[str, str]:
    key = os.environ.get("BYBIT_API_KEY", "").strip()
    secret = os.environ.get("BYBIT_API_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError(
            "BYBIT_API_KEY/BYBIT_API_SECRET не заданы. "
            "Проверь /home/andy/CryptoTrader/.env"
        )
    return key, secret


def bybit_exchange(*, with_auth: bool = True, override_recv_window: Optional[int] = None) -> ccxt.bybit:
    """Возвращает сконфигурированный ccxt.bybit с time-sync.

    Args:
        with_auth: если True — подставляет apiKey/secret из env. Иначе — публичный клиент.
        override_recv_window: подменить recvWindow (для тестов / дебага).
    """
    cfg = {k: v for k, v in _DEFAULTS.items()}
    cfg["options"] = dict(_DEFAULTS["options"])
    if override_recv_window is not None:
        cfg["options"]["recvWindow"] = override_recv_window
    if with_auth:
        key, secret = _require_env()
        cfg["apiKey"] = key
        cfg["secret"] = secret
    ex = ccxt.bybit(cfg)
    # КРИТИЧНО: инициализировать дельту часов ДО первого подписанного запроса
    ex.load_time_difference()
    return ex


def get_usdt_balance() -> tuple[float, float]:
    """Возвращает (free, total) USDT. При ошибке — (0.0, 0.0) + лог."""
    try:
        ex = bybit_exchange(with_auth=True)
        bal = ex.fetch_balance({"type": "swap", "accountType": "UNIFIED"})
        info = (bal.get("USDT") or {})
        free = float(info.get("free", 0.0) or 0.0)
        total = float(info.get("total", 0.0) or 0.0)
        return free, total
    except Exception as e:
        log.warning("get_usdt_balance failed: %s", e)
        return 0.0, 0.0


def fetch_ticker_safe(symbol: str) -> Optional[dict]:
    """Тикер с try/except. Возвращает None при ошибке."""
    try:
        ex = bybit_exchange(with_auth=False)
        return ex.fetch_ticker(symbol)
    except Exception as e:
        log.warning("fetch_ticker(%s) failed: %s", symbol, e)
        return None
