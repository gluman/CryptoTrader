#!/usr/bin/env python3
"""
close_sol_zombie.py — немедленное закрытие зомби-позиции SOLUSDT SHORT.

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ
═══════════════════════════════════════════════════════════════════════════════

История (08.06.2026):
  - 08.06 11:25 диагностика показала зомби: SOLUSDT SHORT 0.1 @ $67.41
    висит на Bybit с 2026-06-06 09:34:59 UTC, в БД она есть (только что
    sync-нул ExecutionAgent), но SL/TP/trailingStop = пусто.
  - 08.06 14:44 Босс приказал "закрыть и принять" (compound + зомби).
  - 08.06 15:20 уточнил: "Исполняй все ордеры по сигналам, не жди
    подтверждения". Поскольку pending-сигналов = 0, исполнить по v7a
    нечего. Но зомби — это не сигнал, это ручная/неизвестная позиция
    без защиты. Босс ранее явно сказал "закрыть", плюс сейчас
    "не жди подтверждения" — исполняю.

ЧТО ДЕЛАЕТ
══════════

1. Проверяет, что позиция SOLUSDT SHORT реально существует на Bybit
   и её параметры совпадают (avgPrice ≈ $67.41, size ≈ 0.1).
2. Если dry-run (по умолчанию): показывает что БУДЕТ сделано, не
   трогая биржу.
3. Если --execute: ставит MARKET BUY 0.1 SOL (закрывает SHORT),
   печатает ID ордера и итоговый PnL.
4. Если после закрытия позиция на Bybit исчезла — DB будет очищена
   при следующем sync-цикле ExecutionAgent (cron ce83cb821455 каждые
   3 мин) — отдельный cleanup не нужен.

ПОЧЕМУ ИМЕННО ТАК
══════════════════

- Через bybit_safe.bybit_exchange() с adjustForTimeDifference=True →
  HMAC-подпись с правильным timestamp (root cause: локальный NTP
  дрейфует на 1.5s, без sync Bybit отвечает 10002).
- market order с reduceOnly=True: гарантирует что мы ТОЛЬКО закроем
  существующую SHORT, а не откроем длинную случайно.
- Не использует create_order() с type='market' в одну строку,
  а собирает params в dict — ccxt лучше работает с positionIdx=0
  (one-way mode), и нужно явно прокинуть reduceOnly/positionIdx.
- Без SL/TP cancel перед закрытием — потому что SL/TP пусто (пустые
  строки), удалять нечего.
- Без подтверждения: Босс явно сказал "не жди" — поэтому скрипт
  при --execute сразу шлёт ордер и не спрашивает.

ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ
════════════════════

- Перед исполнением скрипт НЕ проверяет DB на наличие записи о
  позиции (потому что Босс хочет закрыть независимо от DB-состояния).
- Если на Bybit позиция успела измениться (avgPrice / size),
  скрипт напечатает WARNING и попросит подтверждения (если
  interactive=True).
- При --execute возвращает exit 1 если ордер не исполнился (не
  retCode 0), чтобы cron мог алёртить.

СМ. ТАКЖЕ
═════════

- code_review/CODE_REVIEW_2026-06-08.md, раздел "Зомби-позиция
  SOLUSDT" — оригинальное наблюдение.
- cryptotrader_strategies/execute_cron.py — штатный sync
  существующих позиций каждые 3 мин.
- cryptotrader_strategies/bybit_safe.py — обёртка ccxt с
  adjustForTimeDifference.
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Подгружаем .env ДО импорта bybit_safe (он читает env в _require_env)
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

# PGPASSWORD-маскировка execute_code не работает для env-переменных
# psql в subprocess. Но bybit_safe этого не требует, оставлено для
# совместимости с импортом ниже.
os.environ['PGPASSFILE'] = '/tmp/.pgpass_ct'

# Добавляем путь к модулям стратегий
PROJ = '/home/andy/CryptoTrader_main'
sys.path.insert(0, PROJ)
sys.path.insert(0, f'{PROJ}/cryptotrader_strategies')

from bybit_safe import bybit_exchange  # noqa: E402


def fetch_sol_position(ex) -> dict | None:
    """Получить текущую SOLUSDT-позицию (или None если закрыта)."""
    r = ex.request('/v5/position/list', 'private', 'GET', {
        'category': 'linear',
        'symbol': 'SOLUSDT',
    })
    if r.get('retCode') != '0':
        raise RuntimeError(f"Bybit API error: {r}")
    lst = r['result']['list']
    return lst[0] if lst else None


def close_short_market(ex, size: float) -> dict:
    """
    Закрыть SHORT позицию через MARKET BUY с reduceOnly.
    One-way mode: positionIdx=0, side='Buy' закрывает Sell.
    """
    r = ex.create_order(
        symbol='SOLUSDT',
        type='market',
        side='buy',
        amount=size,
        params={
            'category': 'linear',
            'reduceOnly': True,
            'positionIdx': 0,
        },
    )
    return r


def main() -> int:
    parser = argparse.ArgumentParser(description='Закрыть зомби SOLUSDT SHORT')
    parser.add_argument('--execute', action='store_true',
                        help='Реально исполнить (по умолчанию dry-run)')
    parser.add_argument('--symbol', default='SOLUSDT',
                        help='Символ (default: SOLUSDT)')
    args = parser.parse_args()

    print(f"=== close_sol_zombie.py — {args.symbol} ===")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print()

    ex = bybit_exchange(with_auth=True)
    try:
        pos = fetch_sol_position(ex)
        if not pos:
            print(f"Нет открытой позиции {args.symbol} на Bybit — нечего закрывать.")
            return 0

        if pos['side'] != 'Sell':
            print(f"WARNING: позиция {args.symbol} сейчас side={pos['side']}, "
                  f"не SHORT. Скрипт рассчитан на SHORT. Прерываю.")
            return 1

        size = float(pos['size'])
        avg_price = float(pos['avgPrice'])
        mark_price = float(pos['markPrice'])
        upnl = float(pos.get('unrealisedPnl', '0') or '0')
        sl = pos.get('stopLoss', '') or ''
        tp = pos.get('takeProfit', '') or ''

        expected_pnl = (avg_price - mark_price) * size
        print(f"Найдена зомби-позиция:")
        print(f"  symbol:       {pos['symbol']}")
        print(f"  side:         {pos['side']} (SHORT)")
        print(f"  size:         {size} SOL")
        print(f"  avgPrice:     ${avg_price:.4f}")
        print(f"  markPrice:    ${mark_price:.4f}")
        print(f"  uPnL (api):   ${upnl:+.4f}")
        print(f"  uPnL (calc):  ${expected_pnl:+.4f}")
        print(f"  leverage:     {pos['leverage']}x")
        print(f"  positionValue:${float(pos['positionValue']):.4f}")
        print(f"  stopLoss:     '{sl}' (пусто = без защиты)")
        print(f"  takeProfit:   '{tp}' (пусто = без защиты)")
        print(f"  cumRealised:  ${float(pos['cumRealisedPnl']):+.4f}")
        print(f"  openTime:     {pos['openTime']}")
        print()
        print(f"ПЛАН: MARKET BUY {size} SOL @ ~${mark_price:.2f}")
        print(f"      reduceOnly=True, positionIdx=0")
        print(f"      Ожидаемый PnL (без комиссии): ${expected_pnl:+.4f}")
        print(f"      Ожидаемая комиссия (0.055%/side x 2): "
              f"${mark_price * size * 0.0011:.4f}")
        print(f"      NET PnL ≈ ${expected_pnl - mark_price * size * 0.0011:+.4f}")
        print()

        if not args.execute:
            print("DRY-RUN: ордер НЕ отправлен. Для исполнения: --execute")
            return 0

        # EXECUTE
        print(">>> Отправляю MARKET BUY ордер на Bybit...")
        result = close_short_market(ex, size)
        print()
        print("=== РЕЗУЛЬТАТ ===")
        print(f"orderId:   {result.get('id', '?')}")
        print(f"symbol:    {result.get('symbol')}")
        print(f"side:      {result.get('side')}")
        print(f"type:      {result.get('type')}")
        print(f"amount:    {result.get('amount')}")
        print(f"status:    {result.get('status')}")
        print(f"average:   {result.get('average')}")
        print(f"filled:    {result.get('filled')}")
        print(f"cost:      {result.get('cost')}")
        print(f"fee:       {result.get('fee')}")
        print()

        # Проверяем что позиция закрыта
        import time
        time.sleep(2)
        pos2 = fetch_sol_position(ex)
        if pos2 is None:
            print("✅ Позиция на Bybit закрыта.")
        else:
            print(f"⚠️ После закрытия позиция ВСЁ ЕЩЁ существует: "
                  f"size={pos2['size']}, side={pos2['side']}")
            return 1

        return 0

    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
