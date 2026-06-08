#!/usr/bin/env python3
"""
close_sol_zombie.py — ЗАКРЫТЬ зомби-позицию SOLUSDT SHORT 0.1 @ $67.41.

Использование:
    1. DRY-RUN (по умолчанию): python close_sol_zombie.py
       → показывает что БУДЕТ сделано, без реального ордера
    2. РЕАЛЬНО: python close_sol_zombie.py --execute
       → отправляет market BUY 0.1 SOLUSDT, закрывает short

Сценарий: Босс 08.06.2026 решил "закрыть и принять" — зомби-позиция,
открытая 2026-06-07 22:14 UTC, не из clone5-контура, в DB не записана.
Предположительно — ручной/ad-hoc трейд.

После закрытия: orphan-sync в ExecutionAgent (cron execute_cron.sh
каждые 3 мин) подхватит изменение. Но в БД этой позиции нет — orphan
sync смотрит только DB-OPEN позиции. Так что в DB так и останется
"0 OPEN", и на DB-стороне ничего не изменится.
"""
from __future__ import annotations

import os
import sys
import argparse
from datetime import datetime, timezone

sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import ccxt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true',
                        help='Реально исполнить (по умолчанию DRY-RUN)')
    args = parser.parse_args()

    ex = ccxt.bybit({
        'options': {'defaultType': 'linear'},
        'apiKey': os.environ['BYBIT_API_KEY'],
        'secret': os.environ['BYBIT_API_SECRET'],
        'enableRateLimit': True,
    })
    ex.load_time_difference()

    # Шаг 1: проверяем что позиция ещё жива
    print('=== STEP 1: проверяем позицию ===')
    positions = ex.fetch_positions()
    target = None
    for p in positions:
        amt = float(p.get('contracts', 0))
        if amt != 0 and p['symbol'] == 'SOL/USDT:USDT':
            target = p
            break

    if not target:
        print('  Нет открытой SOLUSDT позиции — нечего закрывать.')
        return 0

    print(f"  {target['symbol']} {target['side']} contracts={target['contracts']} "
          f"entry={target['entryPrice']} mark={target['markPrice']} "
          f"uPnL={target.get('unrealisedPnl', '?')} lev={target.get('leverage', '?')}x")

    # Шаг 2: определяем qtyStep
    market = ex.market('SOL/USDT:USDT')
    qty_step = float(market['info']['lotSizeFilter']['qtyStep'])
    print(f'  qtyStep = {qty_step}')

    # Шаг 3: market BUY (закрывает SHORT)
    close_side = 'buy'  # закрываем SHORT → покупаем
    qty = float(target['contracts'])
    # Нормализация к qtyStep
    qty = float(f"{round(qty / qty_step) * qty_step:.10f}".rstrip('0').rstrip('.') or 0)

    print()
    print('=== STEP 2: что будет сделано ===')
    print(f"  Ордер: MARKET {close_side.upper()} {qty} SOLUSDT")
    print(f"  Текущая цена (mark): {target['markPrice']}")
    print(f"  Ожидаемое исполнение: ~{float(target['markPrice']) * qty:.2f} USDT")
    print(f"  Ожидаемый PnL: +${(float(target['entryPrice']) - float(target['markPrice'])) * qty:.3f}")
    print()

    if not args.execute:
        print('=== DRY-RUN (реально НЕ исполняю) ===')
        print('  Для исполнения запусти: python close_sol_zombie.py --execute')
        return 0

    # Шаг 4: реально исполняем
    print('=== STEP 3: ИСПОЛНЯЮ (--execute) ===')
    try:
        order = ex.create_order(
            symbol='SOL/USDT:USDT',
            type='market',
            side=close_side,
            amount=qty,
            params={'reduceOnly': True, 'category': 'linear'},
        )
        print(f"  ✅ Ордер исполнен!")
        print(f"     id={order.get('id', '?')}")
        print(f"     filled={order.get('filled', qty)} @ avg={order.get('average', '?')}")
        print(f"     status={order.get('status', '?')}")
        print()
        print('=== STEP 4: проверяю что позиция закрыта ===')
        positions_after = ex.fetch_positions()
        for p in positions_after:
            amt = float(p.get('contracts', 0))
            if p['symbol'] == 'SOL/USDT:USDT' and amt != 0:
                print(f"  ⚠️ Позиция ВСЁ ЕЩЁ открыта: {p['contracts']} contracts")
                return 1
        print('  ✅ Позиция SOLUSDT закрыта на Bybit.')
        print('  Примечание: в БД этой позиции НЕ БЫЛО (orphan-sync не сработает).')
        print('  Ручной трейд завершён, средства освобождены.')
        return 0
    except Exception as e:
        print(f"  ❌ ОШИБКА: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
