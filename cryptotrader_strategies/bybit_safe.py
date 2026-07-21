"""
bybit_safe.py — единая обёртка для ccxt-инициализации с time-sync.

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ
═══════════════════════════════════════════════════════════════════════════════

История: code review `code_review/CODE_REVIEW_2026-06-08.md` выявил три
критические/высокие проблемы, повторявшиеся в 4 разных скриптах:

  H1 — compound_engine.py
    Создавал ccxt.bybit({...}) БЕЗ `adjustForTimeDifference=True` и БЕЗ
    `load_time_difference()`. Параметр `recvWindow=60000` передавался
    ВЕРХНИМ уровнем конструктора ccxt.bybit({...}), но ccxt ожидает
    `options.recvWindow` — поэтому в ошибке от Bybit приходил
    `recv_window[5000]` (дефолт), а не 60000. Симптом: 10002
    ("invalid request, please check your server timestamp or recv_window
    param"), `get_bybit_balance()` всегда возвращал 0.0 → весь compound
    механизм (tiers, pos_usdt) был мёртв.

  H5 — multi_strategy_monitor.py
    `ccxt.bybit({...})` создавался ВООБЩЕ без apiKey/secret (это был
    забытый публичный клиент), приватный `fetch_balance` падал → `except`
    молча возвращал `(0.0, 0.0)`. Cron-отчёт в Telegram показывал
    "Balance: $0.00 total $0.00 free" каждый час. Вводит в заблуждение.

  H6 — глобально
    Хост srv-cryptotrader опережает сервер Bybit на ~1.5 секунды (дрейф
    NTP отключён). Без `adjustForTimeDifference=True` все подписанные
    запросы получают timestamp из локального `time.time()`, который
    > server_timestamp + recvWindow → отказ 10002.

═══════════════════════════════════════════════════════════════════════════════
ПОЧЕМУ ИМЕННО ТАК, А НЕ ИНАЧЕ
═══════════════════════════════════════════════════════════════════════════════

  1. `adjustForTimeDifference=True` в `options`:
     ccxt при создании экземпляра запоминает флаг. Перед КАЖДЫМ
     подписанным запросом ccxt внутри вычитает сохранённую дельту из
     `time.time()` если флаг включён. Это — единственный способ получить
     timestamp, синхронный с сервером Bybit, без правки системных часов
     и без sudo.

  2. `ex.load_time_difference()`:
     Явный вызов при инициализации — вычисляет разницу
     `serverTime - localTime` по публичному эндпоинту Bybit
     `/v5/market/time`. ccxt сохраняет её внутри. Без ЭТОГО вызова флаг
     `adjustForTimeDifference` остаётся неинициализированным и не
     работает (это документированный gotcha ccxt).

  3. `options.recvWindow=60000`:
     Bybit по умолчанию принимает запросы с timestamp не старше 5 секунд
     (recv_window=5000). При дрейфе 1.5s + задержках сети в 200-500ms
     запросы с локальным timestamp падают. 60000 ms (60s) — щедрый
     запас. Bybit уважает `recvWindow` от клиента.

  4. `defaultType: 'linear'`:
     Bybit V5 разделяет spot / linear (USDT perpetual) / inverse /
     option. Без явного defaultType `fetch_balance` и `fetch_ticker`
     могут вернуть данные из другой категории (или вообще упасть
     10001). Все наши стратегии — linear (perpetual futures на USDT).

  5. `enableRateLimit=True`:
     ccxt встроенный rate-limiter (по умолчанию Bybit = 10 req/s для
     публичных, 5 req/s для приватных). Без него при пакетной обработке
     (10 символов параллельно) упираемся в 429 Too Many Requests.

  6. `timeout=10000`:
     Bybit иногда отвечает по 3-5 секунд (особенно в моменты волатильности).
     Дефолт ccxt 10000 ms оставлен, чтобы не маскировать проблемы сети
     бесконечными зависаниями.

  7. `_require_env()`:
     Вместо `os.environ['KEY']` (KeyError + crash) — `os.environ.get(...,
     '')` + понятный `RuntimeError` с подсказкой, что смотреть в
     /home/andy/CryptoTrader/.env. Согласуется с замечанием L7 code
     review: «падать с понятной ошибкой, а не с KeyError».

  8. `get_usdt_balance()` возвращает (free, total):
     Compound engine исторически ждёт пару `free, total`. Поменяли бы
     сигнатуру — пришлось бы править 4 файла. Оставлено для совместимости.

═══════════════════════════════════════════════════════════════════════════════
ПРИМЕНЕНИЕ
═══════════════════════════════════════════════════════════════════════════════

  from cryptotrader_strategies.bybit_safe import bybit_exchange, get_usdt_balance, fetch_ticker_safe

  # Приватный клиент (подписанные запросы)
  ex = bybit_exchange(with_auth=True)
  bal = ex.fetch_balance({'type': 'swap', 'accountType': 'UNIFIED'})

  # Публичный клиент (только публичные данные)
  ex = bybit_exchange(with_auth=False)
  ticker = ex.fetch_ticker('BTCUSDT')

  # Helper с try/except и логированием
  free, total = get_usdt_balance()
  ticker = fetch_ticker_safe('ETHUSDT')

═══════════════════════════════════════════════════════════════════════════════
ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ
═══════════════════════════════════════════════════════════════════════════════

  1. `adjustForTimeDifference` не поможет если `recvWindow < |delta|`.
     При дельте 1.5s + timeout 0.5s = 2s, recvWindow=5000 должно хватить
     в 99.9% случаев. Мы ставим 60000 для запаса.

  2. Если Bybit меняет API endpoint /v5/market/time — load_time_difference
     упадёт с connection error. Тогда fetch_balance/fetch_ticker всё равно
     упадут (потому что они ходят на /v5/*). Дополнительной обработки
     не нужно.

  3. Bybit rate-limit 1310 (Weekly/Monthly OHLCV Limit Exhausted) — это
     про OHLCV (свечи), а НЕ про balance/ticker/order. fetch_balance
     и fetch_ticker не подпадают. Если в будущем Bybit введёт отдельный
     лимит на приватные запросы — `enableRateLimit` + `recvWindow` всё
     равно защитят.

═══════════════════════════════════════════════════════════════════════════════
СМ. ТАКЖЕ
═══════════════════════════════════════════════════════════════════════════════

  • code_review/CODE_REVIEW_2026-06-08.md, §0.4 R3 (H6 фикс)
  • code_review/CODE_REVIEW_2026-06-08.md, §H1 (compound_engine)
  • code_review/CODE_REVIEW_2026-06-08.md, §H5 (multi_strategy_monitor)
  • execution_agent.py:942 — _bybit_timestamp() для ручной подписи
    (использует тот же time-sync подход, но без ccxt)
  • execute_cron.py — потребитель этой библиотеки (cron каждые 3 мин)
"""
from __future__ import annotations

import os
import logging
from typing import Optional

import ccxt

log = logging.getLogger(__name__)

# Дефолты ccxt.bybit, настроенные под наши нужды.
# ВАЖНО: копируем dict при каждом вызове (см. bybit_exchange), иначе
# override_recv_window утечёт между вызовами.
_DEFAULTS = {
    "enableRateLimit": True,
    "options": {
        "defaultType": "linear",         # USDT perpetual (все наши стратегии)
        "adjustForTimeDifference": True,  # КРИТИЧНО для подписанных запросов
        "recvWindow": 60000,              # 60s запас (Bybit default 5s)
    },
    "timeout": 10000,  # 10s (Bybit иногда отвечает 3-5s в волатильности)
}


def _require_env() -> tuple[str, str]:
    """Достать Bybit API key/secret из env с авто-выбором по VPN-статусу.

    ════════════════════════════════════════════════════════════════════════
    ПРИОРИТЕТ КЛЮЧЕЙ (21.06.2026 FIX — retCode 10010 "Unmatched IP"):
    ════════════════════════════════════════════════════════════════════════
    Раньше читался ТОЛЬКО BYBIT_API_KEY/SECRET — один ключ. При смене IP
    (VPN on/off, ребут сервера) этот ключ переставал работать (10010),
    и весь compound rebalance падал.

    ТЕПЕРЬ:
      1. Если заданы BYBIT_API_KEY_VPN_ON / _VPN_OFF — вызывается
         bybit_key_selector.select_bybit_key(), который try-init'ит
         каждый ключ через ccxt и возвращает рабочий.
      2. Если VPN-ключи НЕ заданы — fallback на старую логику
         (BYBIT_API_KEY/SECRET напрямую).

    L7 code review: было `os.environ['KEY']` → KeyError. Теперь
    `os.environ.get('', '')` + RuntimeError с подсказкой пути.

    Если `BYBIT_TESTNET=true`, берёт BYBIT_TESTNET_API_KEY/SECRET
    (отдельные ключи для testnet.bybit.com). Это позволяет параллельно
    держать mainnet + testnet конфиги без перезаписи основных.

    ═══ RSA-ключ (приоритет над HMAC) ═══
    Если задан `BYBIT_API_PRIVATE_KEY_PATH` (PEM-файл с RSA-2048 private
    key в формате PKCS#8) — Bybit Trading Skill v1.4.2 рекомендует
    использовать RSA signature вместо HMAC. В этом случае:
      • `key`  = `BYBIT_API_KEY` (публичный ID вида "qR5SvVdu...")
      • `secret` = путь к RSA PEM-файлу (ccxt сам подпишет запрос)
    RSA не совместим с dual-key selector (привязка IP к конкретному
    HMAC-ключу), поэтому при RSA-режиме selector пропускается.
    """
    is_testnet = _is_testnet()
    if is_testnet:
        key = os.environ.get("BYBIT_TESTNET_API_KEY", "").strip()
        secret = os.environ.get("BYBIT_TESTNET_API_SECRET", "").strip()
        rsa_path = os.environ.get("BYBIT_TESTNET_API_PRIVATE_KEY_PATH", "").strip()
        env_hint = "BYBIT_TESTNET_API_KEY/BYBIT_TESTNET_API_SECRET"
    else:
        # ═══ 21.06 FIX: VPN dual-key auto-switch ═══
        # Проверяем наличие VPN-ключей. Если есть — делегируем выбор
        # bybit_key_selector, который try-init'ит каждый ключ и вернёт
        # рабочий (тот, чей IP совпадает с текущим IP сервера).
        vpn_off_key = os.environ.get("BYBIT_API_KEY_VPN_OFF", "").strip()
        vpn_on_key = os.environ.get("BYBIT_API_KEY_VPN_ON", "").strip()
        rsa_path = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH", "").strip()
        env_hint = "BYBIT_API_KEY/BYBIT_API_SECRET"

        # RSA имеет приоритет — пропускаем selector
        if not rsa_path and (vpn_off_key or vpn_on_key):
            try:
                import sys as _sys
                _src = "/home/andy/CryptoTrader"
                if _src not in _sys.path:
                    _sys.path.insert(0, _src)
                from src.core.bybit_key_selector import select_bybit_key
                sel_key, sel_secret = select_bybit_key()
                if sel_key and sel_secret:
                    log.info("bybit_safe: key_selector → %s...", sel_key[:8])
                    return sel_key, sel_secret
                else:
                    log.warning("bybit_safe: key_selector returned (None, None) "
                                "— both VPN keys failed, falling back to BYBIT_API_KEY")
            except Exception as e:
                log.warning("bybit_safe: key_selector error: %s — "
                            "falling back to BYBIT_API_KEY", e)

        key = os.environ.get("BYBIT_API_KEY", "").strip()
        secret = os.environ.get("BYBIT_API_SECRET", "").strip()
    # RSA имеет приоритет (Bybit рекомендует с v5 API)
    if rsa_path:
        if not os.path.exists(rsa_path):
            raise RuntimeError(
                f"BYBIT_API_PRIVATE_KEY_PATH={rsa_path} не найден. "
                f"Сгенерируй: openssl genpkey -algorithm RSA "
                f"-pkeyopt rsa_keygen_bits:2048 -out {rsa_path}"
            )
        if not key:
            raise RuntimeError(
                f"BYBIT_API_PRIVATE_KEY_PATH задан, но BYBIT_API_KEY пуст. "
                f"Bybit API Key — публичный ID (вида 'qR5SvVdu...')."
            )
        # ═══ R7 FIX: ccxt RSA ожидает содержимое PEM, а не путь к файлу ═══
        # Прежде возвращали rsa_path (строка вида "/home/.../key.pem"),
        # но ccxt для RSA-подписи Bybit использует secret = PEM-контент,
        # а НЕ путь. Подпись из пути → Bybit вернёт ошибку аутентификации.
        pem_content = open(rsa_path).read()
        log.info("bybit_safe: RSA auth mode (path=%s, key=%s..., pem_len=%d)",
                 rsa_path, key[:8], len(pem_content))
        return key, pem_content
    # Fallback на HMAC
    if not key or not secret:
        raise RuntimeError(
            f"{env_hint} не заданы. "
            f"Проверь /home/andy/CryptoTrader/.env "
            f"(testnet={'true' if is_testnet else 'false'})"
        )
    return key, secret


def _is_testnet() -> bool:
    """True если BYBIT_ENV=testnet ИЛИ BYBIT_TESTNET=true (legacy alias).

    Соответствует официальной конвенции Bybit Trading Skill v1.4.2:
    https://raw.githubusercontent.com/bybit-exchange/skills/main/SKILL.md
    → "Step 2: Configure Credentials" — `export BYBIT_ENV="testnet" # or "mainnet"`.

    Поддерживаем обе формы:
      • `BYBIT_ENV=testnet`  — официальная (skill v1.4.2+)
      • `BYBIT_TESTNET=true` — наша внутренняя (для обратной совместимости
        с существующими cron'ами и `.env` где BYBIT_TESTNET уже задан)
    """
    env = os.environ.get("BYBIT_ENV", "").strip().lower()
    if env in ("testnet", "demo"):
        return True
    if env in ("mainnet", "prod", "production", ""):
        # Если BYBIT_ENV=mainnet — не смотрим на legacy
        if env == "mainnet":
            return False
    # Legacy BYBIT_TESTNET (true/false/1/0)
    legacy = os.environ.get("BYBIT_TESTNET", "false").strip().lower()
    return legacy in ("true", "1", "yes", "on")


def bybit_exchange(*, with_auth: bool = True, override_recv_window: Optional[int] = None) -> ccxt.bybit:
    """Возвращает сконфигурированный ccxt.bybit с time-sync.

    Args:
        with_auth: если True — подставляет apiKey/secret из env.
            False — публичный клиент (только публичные данные:
            tickers, orderbook, OHLCV). Приватные endpoints (balance,
            positions, create_order) вернут ошибку аутентификации.
        override_recv_window: подменить recvWindow (для тестов / дебага
            конкретного запроса). Например, 5000 — чтобы воспроизвести
            проблему с дефолтным окном Bybit.

    Returns:
        ccxt.bybit instance. Уже с вызванным `load_time_difference()` —
        готов к первому подписанному запросу.

    Raises:
        RuntimeError: если `with_auth=True` и ключи не заданы в env.

    TestNet:
        Если `BYBIT_TESTNET=true` в env, `bybit_exchange` автоматически:
        1. Берёт `BYBIT_TESTNET_API_KEY/SECRET` вместо `BYBIT_API_KEY/SECRET`
        2. Передаёт `sandbox=True` в ccxt (URL → https://api-testnet.bybit.com)
        Это позволяет одной командой переключать mainnet ↔ testnet
        без правки кода. Подходит для E2E-тестов виртуальных сделок.

    Example:
        >>> ex = bybit_exchange()
        >>> bal = ex.fetch_balance({'type': 'swap', 'accountType': 'UNIFIED'})
        >>> # bal['USDT']['free'] → float USDT available
    """
    # Копируем дефолты чтобы override_recv_window не утекал между вызовами
    cfg = {k: v for k, v in _DEFAULTS.items() if k != "options"}
    cfg["options"] = dict(_DEFAULTS["options"])
    if override_recv_window is not None:
        cfg["options"]["recvWindow"] = override_recv_window
    if with_auth:
        key, secret = _require_env()
        cfg["apiKey"] = key
        cfg["secret"] = secret
    # TestNet: переключаем endpoint через ccxt `sandbox=True`.
    # Это работает в ccxt для Bybit — он подставит api-testnet.bybit.com.
    # Без этого будут уходить запросы на mainnet и аутентификация упадёт.
    if _is_testnet():
        cfg["sandbox"] = True
        log.info("bybit_exchange: TESTNET mode (sandbox=True)")
    ex = ccxt.bybit(cfg)
    # КРИТИЧНО: load_time_difference() вычислит serverTime - localTime
    # и сохранит дельту. Без ЭТОГО вызова флаг adjustForTimeDifference
    # бесполезен — это известный gotcha ccxt.
    ex.load_time_difference()
    return ex


def get_usdt_balance() -> tuple[float, float]:
    """Возвращает (free, total) USDT на UNIFIED linear аккаунте.

    При ошибке (10002 / 1310 / network) возвращает `(0.0, 0.0)` и
    пишет WARNING в лог — НЕ throw. Compound/монитор должны показывать
    "0.00" а не падать.

    Returns:
        (free_usdt, total_usdt). Например (10.5, 21.85) — $10.15
        свободно, $11.70 в открытой позиции.

    Note:
        accountType='UNIFIED' — единый аккаунт Bybit V5 (объединяет
        spot/derivatives/options). Если у вас CLASSIC account — смените
        на 'CONTRACT' (для linear только) или опустите параметр.
    """
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
    """Тикер с try/except. Возвращает None при ошибке.

    Args:
        symbol: в формате ccxt (например 'BTC/USDT:USDT' для linear,
        'BTC/USDT' для spot). Или упрощённый 'BTCUSDT' — ccxt
        нормализует.

    Returns:
        dict ccxt-ticker: {symbol, last, bid, ask, volume, timestamp, ...}
        или None при ошибке.

    Example:
        >>> t = fetch_ticker_safe('BTCUSDT')
        >>> if t: print(t['last'])
    """
    try:
        ex = bybit_exchange(with_auth=False)
        return ex.fetch_ticker(symbol)
    except Exception as e:
        log.warning("fetch_ticker(%s) failed: %s", symbol, e)
        return None
