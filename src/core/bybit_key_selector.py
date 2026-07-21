#!/usr/bin/env python3
"""
bybit_key_selector.py — Автовыбор Bybit API ключа по VPN-статусу (IP detection).

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ:
  На сервере два API-ключа Bybit: один привязан к IP через VPN (VPN-on),
  другой — к прямому IP сервера (VPN-off). Когда VPN включается/выключается,
  IP меняется, и нужно использовать соответствующий ключ. Раньше переключение
  было ручным — при рассинхроне все ордера падали с 10010 "Unmatched IP".

ЧТО:
  1. Определяет текущий публичный IP сервера (через API call к ipify).
  2. Пытается инициализировать ccxt.bybit с каждым ключом.
  3. Возвращает рабочий ключ/секрет.
  4. Кеширует результат на 5 минут (IP не меняется чаще).

ПОЧЕМУ TRY-ALL (а не сопоставление IP):
  IP может быть динамическим. Надёжнее просто попробовать ключ и посмотреть,
  работает ли. 10010 = IP mismatch → пробуем следующий. 10003 = ключ revoked.

ПРОБЛЕМЫ:
  • Если оба ключа дают 10003 (invalid) → return None, execution disabled.
  • Сетевой запрос к ipify может быть медленным → кеш 5 мин.

СМ. ТАКЖЕ:
  • config.py — загружает BYBIT_API_KEY_VPN_ON / _VPN_OFF из .env
  • execution_agent.py _init_ccxt_bybit() — использует select_bybit_key()
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# ── Cache: (timestamp, key, secret) — TTL 5 минут ──────────────────────────
_cache: Optional[Tuple[float, str, str]] = None
_CACHE_TTL = 300  # 5 минут


def _try_ccxt_init(api_key: str, api_secret: str) -> Tuple[bool, str]:
    """Попытаться инициализировать ccxt.bybit и получить баланс.

    Returns:
        (True, "") если ключ работает.
        (False, error_msg) если ключ не работает.
    """
    try:
        import ccxt
        ex = ccxt.bybit({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "linear"},
        })
        ex.load_time_difference()
        bal = ex.fetch_balance(params={"type": "swap", "accountType": "UNIFIED"})
        usdt = bal.get("USDT", {})
        free = usdt.get("free", "?")
        log.info(f"bybit_key_selector: key {api_key[:8]}... works, USDT free={free}")
        return True, ""
    except Exception as e:
        err_str = str(e)
        if "10010" in err_str:
            return False, "ip_mismatch"
        elif "10003" in err_str:
            return False, "invalid_key"
        else:
            return False, f"error: {err_str[:120]}"


def select_bybit_key() -> Tuple[Optional[str], Optional[str]]:
    """Выбрать рабочий Bybit API key/secret по текущему IP.

    Логика:
      1. Проверить кеш (TTL 5 мин).
      2. Попробовать BYBIT_API_KEY_VPN_OFF (для VPN-off IP — текущий сценарий).
      3. Если не работает — попробовать BYBIT_API_KEY_VPN_ON.
      4. Если оба не работают — вернуть (None, None).

    Returns:
        (api_key, api_secret) или (None, None) если ни один ключ не работает.
    """
    global _cache

    # Check cache
    if _cache is not None:
        ts, cached_key, cached_secret = _cache
        if time.time() - ts < _CACHE_TTL:
            log.debug(f"bybit_key_selector: cache hit ({cached_key[:8]}...)")
            return cached_key, cached_secret

    # Gather candidate keys
    candidates = []

    # VPN-off key (direct server IP) — primary candidate
    vpn_off_key = os.getenv("BYBIT_API_KEY_VPN_OFF", "")
    vpn_off_sec = os.getenv("BYBIT_API_SECRET_VPN_OFF", "")
    if vpn_off_key and len(vpn_off_key) >= 16:
        candidates.append(("VPN_OFF", vpn_off_key, vpn_off_sec))

    # VPN-on key (VPN IP) — fallback candidate
    vpn_on_key = os.getenv("BYBIT_API_KEY_VPN_ON", "")
    vpn_on_sec = os.getenv("BYBIT_API_SECRET_VPN_ON", "")
    if vpn_on_key and len(vpn_on_key) >= 16:
        candidates.append(("VPN_ON", vpn_on_key, vpn_on_sec))

    # Legacy fallback: BYBIT_API_KEY (without VPN suffix)
    legacy_key = os.getenv("BYBIT_API_KEY", "")
    legacy_sec = os.getenv("BYBIT_API_SECRET", "")
    if legacy_key and len(legacy_key) >= 16:
        if legacy_key not in [c[1] for c in candidates]:
            candidates.append(("LEGACY", legacy_key, legacy_sec))

    if not candidates:
        log.error("bybit_key_selector: no API keys found in env")
        return None, None

    # Try each candidate
    for label, key, secret in candidates:
        ok, reason = _try_ccxt_init(key, secret)
        if ok:
            log.info(f"bybit_key_selector: selected {label} key ({key[:8]}...)")
            _cache = (time.time(), key, secret)
            return key, secret
        else:
            log.warning(f"bybit_key_selector: {label} key ({key[:8]}...) failed: {reason}")

    log.error("bybit_key_selector: ALL keys failed — execution disabled")
    return None, None


def clear_cache():
    """Сбросить кеш (для тестов или ручного переключения)."""
    global _cache
    _cache = None
