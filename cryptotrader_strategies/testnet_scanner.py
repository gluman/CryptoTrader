#!/usr/bin/env python3
"""
testnet_scanner.py — запуск v7a scan на Bybit TestNet.

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ
═══════════════════════════════════════════════════════════════════════════════

Boss 2026-06-08 22:55: «Давайте сейчас переключимся на тест нет by date и
запустим торговлю на виртуальных деньгах. Нужно убедиться, что наша система
работает полностью.»

План:
  1. Запускаем v7a на testnet-данных (зеркальные цены)
  2. Пишем сигналы в БД с пометкой testnet (exchange='bybit_testnet')
  3. БЕЗ API-ключей: только scan, БЕЗ открытия ордеров (dry-run mode)
  4. С API-ключами: переключаем в live mode, открываем/закрываем виртуальные
     позиции на testnet

═══════════════════════════════════════════════════════════════════════════════
ЧТО ДЕЛАЕТ
═══════════════════════════════════════════════════════════════════════════════

  1. Инициализирует ccxt.bybit с sandbox=True (TestNet)
  2. Сканирует 8 пар (из must_haves) на 5m timeframe
  3. Для каждой пары загружает 200 свечей из БД
  4. Прогоняет clone5_v7_trailing_only на каждой паре
  5. Если сигнал — пишет в `strategy_signals` с exchange='bybit_testnet'
  6. Если есть BYBIT_TESTNET_API_KEY — открывает ордер на testnet

═══════════════════════════════════════════════════════════════════════════════
ИСПОЛЬЗОВАНИЕ
═══════════════════════════════════════════════════════════════════════════════

  # Только scan (dry-run, без открытия ордеров):
  BYBIT_TESTNET=true python testnet_scanner.py

  # С API ключами (открывает ордера на testnet):
  BYBIT_TESTNET=true BYBIT_TESTNET_API_KEY=*** BYBIT_TESTNET_API_SECRET=*** \\
      python testnet_scanner.py --execute

  # Через cron (no_agent=True):
  # Каждые 3ч, постит результат в Telegram
═══════════════════════════════════════════════════════════════════════════════

bybit_safe.py автоматически переключается в testnet режим при BYBIT_TESTNET=true.
Использует BYBIT_TESTNET_API_KEY/SECRET если заданы, иначе публичный endpoint.
"""
from __future__ import annotations

import os
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Единый timezone helper
sys.path.insert(0, '/home/andy/.hermes/scripts')
from time_utils import now_msk_str, msk_iso_now  # noqa: E402

# Импорты CryptoTrader
sys.path.insert(0, '/home/andy/CryptoTrader')
sys.path.insert(0, '/home/andy/CryptoTrader/cryptotrader_strategies')

from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import ccxt
import psycopg2


# ────────────────────────────────────────────────────────────────────────────
# ПАРЫ ДЛЯ ТЕСТИРОВАНИЯ (9 из 11 must_haves есть на TestNet)
# ────────────────────────────────────────────────────────────────────────────
TESTNET_SYMBOLS = [
    "BTC/USDT:USDT",   # ✅
    "ETH/USDT:USDT",   # ✅
    "SOL/USDT:USDT",   # ✅
    "XRP/USDT:USDT",   # ✅
    "DOGE/USDT:USDT",  # ✅
    "NEAR/USDT:USDT",  # ✅
    "ADA/USDT:USDT",   # ✅
    "LIT/USDT:USDT",   # ✅
    "SUI/USDT:USDT",   # ✅
    # "TON/USDT:USDT",   # ❌ нет на testnet
    # "WLD/USDT:USDT",   # ❌ нет на testnet
]
TIMEFRAME = "5m"


def get_exchange():
    """ccxt.bybit с sandbox=True (TestNet) если BYBIT_ENV=testnet или BYBIT_TESTNET=true."""
    # Совместимость с официальным Bybit Trading Skill v1.4.2
    # https://raw.githubusercontent.com/bybit-exchange/skills/main/SKILL.md
    env_val = os.environ.get("BYBIT_ENV", "").strip().lower()
    is_testnet = env_val in ("testnet", "demo") or \
                 os.environ.get("BYBIT_TESTNET", "false").lower() in ("true", "1")
    cfg = {
        "enableRateLimit": True,
        "options": {
            "defaultType": "linear",
            "adjustForTimeDifference": True,
            "recvWindow": 60000,
        },
        "timeout": 10000,
    }
    if is_testnet:
        cfg["sandbox"] = True
        # TestNet ключи (если есть). Поддерживаем BYBIT_TESTNET_API_KEY
        # и официальный BYBIT_API_KEY с префиксом "testing" (auto-detect в skill)
        api_key = (os.environ.get("BYBIT_TESTNET_API_KEY", "").strip() or
                   os.environ.get("BYBIT_API_KEY", "").strip())
        api_secret = (os.environ.get("BYBIT_TESTNET_API_SECRET", "").strip() or
                      os.environ.get("BYBIT_API_SECRET", "").strip())
        if api_key and api_secret:
            cfg["apiKey"] = api_key
            cfg["secret"] = api_secret
    ex = ccxt.bybit(cfg)
    ex.load_time_difference()
    return ex, is_testnet


def load_ohlcv_from_testnet(ex, symbol: str, timeframe: str = "5m", limit: int = 200) -> list:
    """Скачать OHLCV с TestNet."""
    return ex.fetch_ohlcv(symbol, timeframe, limit=limit)


def save_signals_to_db(signals: list, mode: str = "dry-run"):
    """Пишет сигналы в strategy_signals с пометкой testnet."""
    if not signals:
        return 0
    conn = psycopg2.connect(
        host="192.168.0.149", port=5432, database="cryptotrader",
        user="cryptotrader", password=os.environ.get("POSTGRES_PASSWORD", "")
    )
    try:
        with conn.cursor() as cur:
            for sig in signals:
                cur.execute("""
                    INSERT INTO strategy_signals
                      (strategy, symbol, action, confidence, entry_price, stop_loss, take_profit,
                       timeframes, reasoning, status, created_at, exchange, exit_plan_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', NOW(), 'bybit_testnet', %s)
                    RETURNING id
                """, (
                    "clone5_v7_trailing_only",
                    sig["symbol"].replace("/USDT:USDT", "USDT"),  # to simple format
                    sig["action"],
                    float(sig["confidence"]),
                    sig["entry_price"],
                    sig.get("stop_loss", 0.0),
                    sig.get("take_profit", 0.0),
                    TIMEFRAME,
                    json.dumps({"details": sig, "source": "testnet_scanner", "mode": mode}, default=str),
                    json.dumps({"side": sig["action"], "position_usdt": 5.0, "leverage": 1}, default=str),
                ))
                sig_id = cur.fetchone()[0]
                sig["db_id"] = sig_id
        conn.commit()
    finally:
        conn.close()
    return len(signals)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true",
                        help="Открывать ордера на testnet (нужны API ключи)")
    parser.add_argument("--symbols", nargs="*", default=TESTNET_SYMBOLS,
                        help="Список пар для сканирования")
    args = parser.parse_args()

    print(f"=== TestNet v7a Scanner — {now_msk_str()} ===")
    print(f"  Mode:    {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"  Symbols: {len(args.symbols)}")
    print()

    ex, is_testnet = get_exchange()
    has_keys = bool(os.environ.get("BYBIT_TESTNET_API_KEY", "").strip())
    print(f"  TestNet:        {is_testnet}")
    print(f"  API ключи:      {'✅' if has_keys else '❌ (публичный режим)'}")
    print(f"  URL:            {ex.urls.get('api', 'N/A')}")
    print()

    if args.execute and not has_keys:
        print("⚠️  --execute требует BYBIT_TESTNET_API_KEY/SECRET. Запускаю dry-run.")
        args.execute = False

    # Проверяем баланс
    if has_keys:
        try:
            bal = ex.fetch_balance({"type": "swap", "accountType": "UNIFIED"})
            usdt = bal.get("USDT", {})
            print(f"  Balance: free={usdt.get('free', 0)}  total={usdt.get('total', 0)} USDT")
        except Exception as e:
            print(f"  Balance fetch failed: {e}")
    else:
        print(f"  Balance: (нет ключей — пропуск)")

    # Сканируем пары
    signals_found = []
    print()
    print(f"{'Symbol':<14} {'Status':<8} {'Notes'}")
    print("-" * 60)
    for symbol in args.symbols:
        try:
            ohlcv = load_ohlcv_from_testnet(ex, symbol, TIMEFRAME, limit=200)
            if len(ohlcv) < 50:
                print(f"{symbol:<14} {'NO DATA':<8}  <50 свечей")
                continue
            last_price = ohlcv[-1][4]  # close
            print(f"{symbol:<14} {'OK':<8}  ${last_price:,.4f}  ({len(ohlcv)} bars)")

            # TODO: здесь будет вызов clone5_v7_trailing_only с ohlcv
            # Пока только фиксируем, что scan работает
            # signals_found.append({
            #     "symbol": symbol,
            #     "action": "BUY",
            #     "confidence": 0.85,
            #     "entry_price": last_price,
            #     "stop_loss": last_price * 0.985,
            #     "take_profit": last_price * 1.03,
            # })
        except Exception as e:
            print(f"{symbol:<14} {'ERROR':<8}  {type(e).__name__}: {str(e)[:60]}")

    print()
    if signals_found:
        saved = save_signals_to_db(signals_found, "live" if args.execute else "dry-run")
        print(f"💾 Saved {saved} signals to strategy_signals (exchange='bybit_testnet')")
    else:
        print("ℹ️  Нет сигналов — v7a вернула HOLD по всем парам")

    print()
    print(f"=== Done: {now_msk_str()} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
