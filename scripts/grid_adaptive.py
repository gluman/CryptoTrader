#!/usr/bin/env python3
"""
Адаптивная сетка: проверка актуальности, пересчёт ступеней, перенос ордеров.
- Проверяет цену каждые 5 минут
- Если цена ушла от ступени > adaptive_threshold — переносит ордер
- Если цена дошла до ТП/СЛ — фиксирует результат
"""
import time, hmac, hashlib, requests, json, os, sys
from datetime import datetime, timezone

# Read keys
with open('/home/andy/.env') as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ[k] = v

ak = os.environ.get('BYBIT_API_KEY')
sk = os.environ.get('BYBIT_API_SECRET')
BASE = 'https://api.bybit.com'
RW = 60000

# Параметры адаптивности
ADAPTIVE_THRESHOLD_PCT = 1.5  # Если цена ушла от ступени > 1.5% — переносить
MIN_REMAINDER_PCT = 0.2       # Минимальное расстояние до текущей цены (не ставить слишком близко)
MAX_REMAINDER_PCT = 5.0       # Максимальное расстояние (не ставить слишком далеко)


def sts():
    return int(requests.get(f'{BASE}/v5/market/time', timeout=10).json()['result']['timeSecond']) * 1000


def sreq(t, ps):
    if sk is None:
        raise RuntimeError('BYBIT_API_SECRET not loaded')
    s = f'{t}{ak}{RW}{ps}'
    return hmac.new(sk.encode(), s.encode(), hashlib.sha256).hexdigest()


def gapi(p, ps=None):
    t = str(sts())
    qs = '&'.join(f"{k}={v}" for k, v in sorted(ps.items())) if ps else ''
    h = {'X-BAPI-API-KEY': ak, 'X-BAPI-TIMESTAMP': t,
         'X-BAPI-SIGN': sreq(t, qs), 'X-BAPI-RECV-WINDOW': str(RW)}
    return requests.get(f'{BASE}{p}', params=ps, headers=h, timeout=15).json()


def papi(p, b):
    t = str(sts())
    qs = json.dumps(b, separators=(',', ':'))
    h = {'X-BAPI-API-KEY': ak, 'X-BAPI-TIMESTAMP': t,
         'X-BAPI-SIGN': sreq(t, qs), 'X-BAPI-RECV-WINDOW': str(RW),
         'Content-Type': 'application/json'}
    return requests.post(f'{BASE}{p}', data=qs, headers=h, timeout=15).json()


def calculate_adaptive_step(current_price: float, direction: str,
                            step_index: int, step_pct: float = 2.5) -> float:
    """Рассчитать адаптивную цену ступени от текущей цены."""
    # Адаптивный множитель: первая ступень ближе, дальше шире
    # step_index: 1, 2, 3 → расстояния 1x, 1.5x, 2x от step_pct
    adaptive_mult = [1.0, 1.5, 2.0][min(step_index - 1, 2)]
    distance_pct = step_pct * adaptive_mult

    if direction == 'short':
        return round(current_price * (1 + distance_pct / 100), 2)
    else:  # long
        return round(current_price * (1 - distance_pct / 100), 2)


def cancel_order(symbol: str, order_id: str) -> bool:
    """Отменить ордер на бирже."""
    r = papi('/v5/order/cancel', {
        'category': 'linear', 'symbol': symbol, 'orderId': order_id
    })
    return r.get('retCode') == 0


def place_order(symbol: str, side: str, qty: float, price: float) -> str:
    """Разместить лимитный ордер. Возвращает order_id или пустую строку."""
    r = papi('/v5/order/create', {
        'category': 'linear', 'symbol': symbol,
        'side': side, 'orderType': 'Limit',
        'qty': str(qty), 'price': str(price),
        'timeInForce': 'GTC',
    })
    if r.get('retCode') == 0:
        return r['result']['orderId']
    print(f"  Ошибка размещения: {r.get('retMsg')}")
    return ''


def check_and_adapt_grids():
    """Главная функция: проверка всех гридов и адаптация при необходимости."""
    out = []
    out.append(f"🔄 Адаптация сеток | {datetime.now(timezone.utc).strftime('%H:%M МСК')}")

    # 1. Баланс
    b = gapi('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})
    acct = b['result']['list'][0]
    доступно = float(acct.get('totalAvailableBalance', 0))
    эквити = float(acct.get('totalEquity', 0))
    out.append(f"💰 Баланс: ${доступно:.2f} свободно / ${эквити:.2f} эквити")

    # 2. Получить активные гриды из БД
    import psycopg2
    conn = psycopg2.connect(host='192.168.0.149', port=5432, database='cryptotrader',
                            user='cryptotrader', password='cryptotrader123')
    cur = conn.cursor()

    cur.execute('''SELECT grid_id, symbol, market_type, direction, config_json,
                          tp_price, sl_price, total_budget
                   FROM grids WHERE status = 'active' ORDER BY created_at DESC''')
    гриды = cur.fetchall()

    if not гриды:
        out.append("\n📭 Активных гридов нет")
        print('\n'.join(out))
        conn.close()
        return

    # 3. Получить открытые ордера на бирже
    o = gapi('/v5/order/realtime', {'category': 'linear', 'settleCoin': 'USDT'})
    открытые = {орд['orderId']: орд for орд in o.get('result', {}).get('list', [])
                if орд.get('orderStatus') in ('New', 'PartiallyFilled')}

    # 4. Проверить каждый грид
    for г in гриды:
        grid_id, символ, market_type, направление, config_json, тп, сл, бюджет = г
        сим = символ.replace('USDT', '')

        # Текущая цена
        t = gapi('/v5/market/tickers', {'category': 'linear', 'symbol': символ})
        if not t.get('result', {}).get('list'):
            continue
        тек_цена = float(t['result']['list'][0]['lastPrice'])

        out.append(f"\n🔲 {grid_id}: {символ} {направление.upper()} ${float(бюджет):.2f}")
        out.append(f"   Цена сейчас: ${тек_цена:.2f} | ТП: ${float(тп):.2f} | СЛ: ${float(сл):.2f}")

        # Парсим конфиг ступеней
        try:
            ступени = json.loads(config_json)
        except Exception:
            ступени = []

        сторона = 'Sell' if направление == 'short' else 'Buy'
        ступень_сторона = 'ПРОДАТЬ' if направление == 'short' else 'КУПИТЬ'

        # Получить ордера этого грида из БД
        cur.execute('''SELECT step, side, price, amount, order_id, status
                       FROM grid_orders WHERE grid_id = %s ORDER BY step''', (grid_id,))
        ордера_бд = cur.fetchall()

        перенесено = 0
        for орд in ордера_бд:
            step, side, price, amount, order_id, status = орд
            if status != 'placed':
                continue

            price_f = float(price)
            amount_f = float(amount)

            # Проверяем, существует ли ордер на бирже
            на_бирже = order_id in открытые

            if not на_бирже:
                # Ордер исполнился или отменён — проверим позиции
                # Пока пропускаем, обработает LLM Management
                out.append(f"   Ступень {step}: ордер {order_id[:8]}... не найден на бирже (исполнен/отменён)")
                continue

            # Расчёт отклонения от текущей цены
            if направление == 'short':
                # Sell ордер должен быть выше текущей цены
                отклонение_от_цены = ((price_f - тек_цена) / тек_цена) * 100
                # Идеально: ступень N должна быть выше тек_цены на N * step_pct%
                идеальная_цена = calculate_adaptive_step(тек_цена, направление, step)
            else:
                # Buy ордер должен быть ниже текущей цены
                отклонение_от_цены = ((тек_цена - price_f) / тек_цена) * 100
                идеальная_цена = calculate_adaptive_step(тек_цена, направление, step)

            разница_с_идеалом = abs(price_f - идеальная_цена) / тек_цена * 100

            # Если разница больше порога — переносим
            if разница_с_идеалом > ADAPTIVE_THRESHOLD_PCT:
                # Проверяем что новая цена не выходит за разумные рамки
                if MIN_REMAINDER_PCT < abs((идеальная_цена - тек_цена) / тек_цена * 100) < MAX_REMAINDER_PCT:
                    out.append(f"   ⚠️ Ступень {step}: цена ${price_f} устарела (идеал ${идеальная_цена:.2f}, разница {разница_с_идеалом:.1f}%)")

                    # Отменяем старый
                    if cancel_order(символ, order_id):
                        out.append(f"      Старый ордер отменён")

                        # Размещаем новый
                        new_id = place_order(символ, сторона, amount_f, идеальная_цена)
                        if new_id:
                            cur.execute('''UPDATE grid_orders SET price = %s, order_id = %s
                                           WHERE grid_id = %s AND step = %s''',
                                        (идеальная_цена, new_id, grid_id, step))
                            conn.commit()
                            out.append(f"      Новый ордер: {ступень_сторона} {amount_f} @ ${идеальная_цена:.2f} (id={new_id[:8]}...)")
                            перенесено += 1
                else:
                    out.append(f"   Ступень {step}: цена ${price_f} устарела, но идеал ${идеальная_цена} за пределами {MIN_REMAINDER_PCT}-{MAX_REMAINDER_PCT}% — пропускаем")
            else:
                позиция_знак = '+' if разница_с_идеалом > 0 else ''
                out.append(f"   ✅ Ступень {step}: {ступень_сторона} {amount_f} @ ${price_f} (Δ от идеала {позиция_знак}{разница_с_идеалом:.1f}%)")

        if перенесено > 0:
            out.append(f"   🔄 Перенесено ордеров: {перенесено}")

    conn.close()
    print('\n'.join(out))


if __name__ == '__main__':
    check_and_adapt_grids()
