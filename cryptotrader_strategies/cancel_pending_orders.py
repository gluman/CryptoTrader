#!/usr/bin/env python3
"""
cancel_pending_orders.py — отмена висящих pending-ордеров.

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ
═══════════════════════════════════════════════════════════════════════════════

Boss 08.06.2026 15:58 приказал: «удали эти отложенные ордера, думаю они не
актуальны и остались от старой стратегии». Речь о 4 sell-limit ордерах
SOLUSDT, обнаруженных при диагностике баланса:

  0541f11b... 0.1 SOL SELL limit $68.07 (3.4 дня в рынке)
  c58668f5... 0.1 SOL SELL limit $72.88 (4.3 дня)
  a94a4a54... 0.1 SOL SELL limit $71.15 (4.3 дня)
  95d6e647... 0.1 SOL SELL limit $71.22 (4.3 дня)

Эти ордера блокируют ~$9.58 USDT (REGULAR_MARGIN risk-limit reserve), и
не дают боту открывать новые SHORT (если они появятся). Наш бот
(`clone5_v7_trailing_only`) работает ТОЛЬКО с MARKET ордерами — он их не
создавал. Остались от старой стратегии или ручного скрипта.

ЧТО ДЕЛАЕТ
══════════

1. Показывает все висящие regular-ордера (dry-run по умолчанию).
2. По --execute отменяет через Bybit V5 API
   `/v5/order/cancel` с `orderId` для каждого ордера.
3. После отмены показывает remaining orders, чтобы убедиться, что все
   сняты.

ПОЧЕМУ ИМЕННО ТАК
══════════════════

- Через `ex.request('/v5/order/cancel', 'private', 'POST', ...)` — это
  правильный endpoint, у Bybit cancel для лимитных ордеров отдельный от
  cancel-all.
- По одному orderId за раз — Bybit V5 не поддерживает batch cancel
  regular orders (есть только `cancel-all` для условных через
  `stopOrderType`). Лучше точечно, чтобы получить explicit retCode
  на каждый.
- Не использует `cancel_all_orders` — слишком грубо, может зацепить
  что-то, чего не видим.
- Idempotent: если ордер уже отменён, Bybit вернёт 17001 (Order not
  exists/found) или подобный — мы это логируем как warning, не error.
- dry-run default: Boss может сначала увидеть план, потом явно
  запустить с --execute.

ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ
════════════════════

- Скрипт работает ТОЛЬКО с linear (USDT perp) ордерами. Если у Босса
  висят spot/inverse ордера — нужен второй прогон с другим category.
- Не трогает conditional (SL/TP) ордера. Если нужны — отдельный
  скрипт.
- Не отменяет TP/SL, привязанные к открытой позиции. На текущий
  момент открытых позиций нет (verified 08.06.2026 15:58), поэтому
  таких ордеров быть не должно.

СМ. ТАКЖЕ
═════════

- cryptotrader_strategies/close_sol_zombie.py — аналогичный скрипт
  для закрытия зомби-позиции (использовался ранее сегодня).
- cryptotrader_strategies/multi_strategy_monitor.py — теперь
  показывает pending-ордера в breakdown.
- bybit_safe.py — обёртка ccxt с adjustForTimeDifference=True.
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

# Единый timezone helper (MSK UTC+3) для отчётов Boss
sys.path.insert(0, '/home/andy/.hermes/scripts')
from time_utils import now_msk_str, to_msk_str  # noqa: E402

from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

os.environ['PGPASSFILE'] = '/tmp/.pgpass_ct'

PROJ = '/home/andy/CryptoTrader_main'
sys.path.insert(0, PROJ)
sys.path.insert(0, f'{PROJ}/cryptotrader_strategies')

from bybit_safe import bybit_exchange  # noqa: E402


def fetch_pending(ex) -> list[dict]:
    r = ex.request('/v5/order/realtime', 'private', 'GET', {
        'category': 'linear', 'settleCoin': 'USDT', 'limit': 50,
    })
    return r.get('result', {}).get('list', [])


def cancel_one(ex, symbol: str, order_id: str) -> dict:
    return ex.request('/v5/order/cancel', 'private', 'POST', {
        'category': 'linear',
        'symbol': symbol,
        'orderId': order_id,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description='Отменить pending-ордера (linear)')
    parser.add_argument('--execute', action='store_true',
                        help='Реально отменить (по умолчанию dry-run)')
    parser.add_argument('--symbol', default=None,
                        help='Фильтр по символу (default: все linear)')
    parser.add_argument('--order-id', default=None,
                        help='Отменить конкретный orderId (default: все)')
    args = parser.parse_args()

    print(f"=== cancel_pending_orders.py ===  {now_msk_str()}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"Filter: symbol={args.symbol or 'ALL'}, order_id={args.order_id or 'ALL'}")
    print()

    ex = bybit_exchange(with_auth=True)
    try:
        orders = fetch_pending(ex)
        if not orders:
            print("Нет pending-ордеров — нечего отменять.")
            return 0

        # Применить фильтры
        if args.symbol:
            orders = [o for o in orders if o['symbol'] == args.symbol]
        if args.order_id:
            orders = [o for o in orders if o['orderId'] == args.order_id]

        if not orders:
            print("После фильтрации ордеров не осталось.")
            return 0

        print(f"Найдено ордеров к отмене: {len(orders)}")
        for i, o in enumerate(orders, 1):
            ctime = int(o.get('createdTime', 0)) / 1000
            age_h = (datetime.now(timezone.utc).timestamp() - ctime) / 3600
            print(f"  [{i}] {o['symbol']:10} {o['side']:4} {o['orderType']:10} "
                  f"qty={o['qty']:>8} price=${o.get('price',''):>10} "
                  f"age={age_h:.0f}h  orderId={o['orderId']}")
        print()

        if not args.execute:
            print("DRY-RUN: ордера НЕ отменены. Для исполнения: --execute")
            return 0

        # EXECUTE
        print(">>> Отправляю cancel-запросы на Bybit...")
        ok, fail = 0, 0
        for o in orders:
            try:
                r = cancel_one(ex, o['symbol'], o['orderId'])
                ret = r.get('retCode')
                if ret == 0 or ret == '0':
                    print(f"  ✅ {o['orderId'][:8]}... ({o['symbol']} {o['side']} "
                          f"{o['qty']} @ ${o.get('price','')}) отменён")
                    ok += 1
                else:
                    # 17001 = order not found (уже отменён/исполнен) — не error
                    print(f"  ⚠️  {o['orderId'][:8]}... retCode={ret} "
                          f"retMsg={r.get('retMsg', '')}")
                    if str(ret) == '17001':
                        ok += 1  # уже снят — считаем успехом
                    else:
                        fail += 1
            except Exception as e:
                print(f"  ❌ {o['orderId'][:8]}... ERROR: {type(e).__name__}: {e}")
                fail += 1

        print()
        print(f"=== ИТОГ: ok={ok}, fail={fail} ===")

        # Проверка остатка
        import time
        time.sleep(1)
        remaining = fetch_pending(ex)
        print(f"Осталось pending-ордеров: {len(remaining)}")
        if remaining:
            print("Оставшиеся:")
            for o in remaining:
                print(f"  - {o['symbol']} {o['side']} {o['orderType']} "
                      f"qty={o['qty']} price=${o.get('price','')} "
                      f"orderId={o['orderId'][:8]}")

        return 0 if fail == 0 else 1

    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
