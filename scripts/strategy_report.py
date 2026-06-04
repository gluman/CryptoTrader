#!/usr/bin/env python3
"""Стратегический отчёт: баланс, грид, мартингейл, кросс-пары, TV прогнозы."""
import time, hmac, hashlib, requests, json, os, sys
from datetime import datetime, timezone

with open('/home/andy/.env') as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ[k] = v

ak = os.environ.get('BYBIT_API_KEY')
sk = os.environ.get('BYBIT_API_SECRET')
BASE = 'https://api.bybit.com'
RW = 60000


def sts():
    return int(requests.get(f'{BASE}/v5/market/time', timeout=10).json()['result']['timeSecond']) * 1000


def sreq(t, ps):
    s = f'{t}{ak}{RW}{ps}'
    return hmac.new(sk.encode(), s.encode(), hashlib.sha256).hexdigest()


def gapi(p, ps=None):
    t = str(sts())
    qs = '&'.join(f"{k}={v}" for k, v in sorted(ps.items())) if ps else ''
    h = {'X-BAPI-API-KEY': ak, 'X-BAPI-TIMESTAMP': t,
         'X-BAPI-SIGN': sreq(t, qs), 'X-BAPI-RECV-WINDOW': str(RW)}
    return requests.get(f'{BASE}{p}', params=ps, headers=h, timeout=15).json()


def main():
    out = []
    ts_сейчас = datetime.now(timezone.utc).strftime('%H:%M МСК')
    out.append(f"📊 Отчёт по стратегии | {ts_сейчас}")

    # Баланс
    b = gapi('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})
    acct = b['result']['list'][0]
    доступно = float(acct.get('totalAvailableBalance', 0))
    эквити = float(acct.get('totalEquity', 0))
    в_марже = эквити - доступно
    out.append(f"💰 Баланс: ${доступно:.2f} свободно / ${эквити:.2f} всего (в марже ${в_марже:.2f})")

    # Позиции
    p = gapi('/v5/position/list', {'category': 'linear', 'settleCoin': 'USDT'})
    позы = [поз for поз in p.get('result', {}).get('list', []) if float(поз.get('size', 0)) > 0]

    if позы:
        out.append("\n📍 Открытые позиции:")
        for поз in позы:
            сим = поз['symbol'].replace('USDT', '')
            пнл = float(поз.get('unrealisedPnl', 0))
            вход = float(поз['entryPrice'])
            текущая = float(поз.get('markPrice', вход))
            сторона = 'ЛОНГ' if поз['side'] == 'Buy' else 'ШОРТ'
            изм = ((текущая - вход) / вход * 100) * (1 if сторона == 'ЛОНГ' else -1)
            out.append(f"  {сторона} {сим} кол-во={поз['size']} вход=${вход:.2f} → ${текущая:.2f} ({изм:+.2f}%) пнл=${пнл:+.4f}")
    else:
        out.append("\n📍 Позиций нет")

    # Активные ордера
    o = gapi('/v5/order/realtime', {'category': 'linear', 'settleCoin': 'USDT'})
    ордера = [орд for орд in o.get('result', {}).get('list', []) if орд.get('orderStatus') in ('New', 'PartiallyFilled')]

    if ордера:
        out.append("\n⏳ Активные ордера:")
        for орд in ордера:
            сим = орд['symbol'].replace('USDT', '')
            сторона = 'КУПИТЬ' if орд['side'] == 'Buy' else 'ПРОДАТЬ'
            out.append(f"  {сторона} {сим} {орд['qty']} @ ${орд['price']}")
    else:
        out.append("\n⏳ Активных ордеров нет")

    # Гриды из БД
    import psycopg2
    conn = psycopg2.connect(host='192.168.0.149', port=5432, database='cryptotrader',
                            user='cryptotrader', password='cryptotrader123')
    cur = conn.cursor()

    cur.execute('''SELECT grid_id, symbol, direction, total_budget, tp_price, sl_price
                   FROM grids WHERE status = 'active' ORDER BY created_at DESC''')
    гриды = cur.fetchall()

    if гриды:
        out.append("\n🔲 Активные гриды:")
        for г in гриды:
            бюджет = float(г[3] or 0)
            тп = float(г[4] or 0)
            сл = float(г[5] or 0)
            направление = 'ШОРТ' if г[2] == 'short' else 'ЛОНГ'
            out.append(f"  {г[0]}: {г[1]} {направление} ${бюджет:.2f} | ТП ${тп} | СЛ ${сл}")

    # Цена SOL
    sol_сейчас = 0
    sol_24ч = 0
    t = gapi('/v5/market/tickers', {'category': 'linear', 'symbol': 'SOLUSDT'})
    if t.get('result', {}).get('list'):
        sol_сейчас = float(t['result']['list'][0]['lastPrice'])
        sol_24ч = float(t['result']['list'][0].get('price24hPcnt', 0)) * 100
        out.append(f"\n📈 SOL: ${sol_сейчас:.2f} (за 24ч {sol_24ч:+.2f}%)")

        # Анализ и обоснование
        if гриды:
            тп_первого = float(гриды[0][4])
            разница_до_тп = ((sol_сейчас - тп_первого) / sol_сейчас) * 100
            статус_тп = 'уже в профите' if sol_сейчас <= тп_первого else 'нужно падение'
            out.append(f"💡 До ТП ${(sol_сейчас - тп_первого):.2f} ({разница_до_тп:+.1f}%) — {статус_тп}")

    # Кулдауны
    cur.execute("SELECT symbol, cooldown_until FROM martingale_cooldowns WHERE cooldown_until > NOW()")
    кулдауны = cur.fetchall()
    if кулдауны:
        out.append("\n⏸ Кулдауны мартингейла:")
        for к in кулдауны:
            out.append(f"  {к[0]} до {к[1].strftime('%H:%M МСК')}")

    # Кросс-парный анализ
    cur.execute('''SELECT base_symbol, vs_btc_24h, vs_eth_24h, momentum_24h
                   FROM cross_pair_analysis
                   WHERE created_at > NOW() - INTERVAL '1 hour'
                   ORDER BY momentum_24h DESC LIMIT 5''')
    кроссы = cur.fetchall()
    if кроссы:
        out.append("\n🔄 Кросс-пары (сильнейшие за 24ч):")
        for к in кроссы:
            знак = '🟢' if float(к[3]) > 0 else '🔴'
            out.append(f"  {знак} {к[0]}: vs BTC {float(к[1]):+.2f}% | vs ETH {float(к[2]):+.2f}% | моментум {float(к[3]):+.2f}%")

    # TV прогнозы
    cur.execute('''SELECT analyst, COUNT(*),
                          SUM(CASE WHEN outcome='correct' THEN 1 ELSE 0 END) as верных
                   FROM tv_predictions
                   WHERE outcome IS NOT NULL
                   GROUP BY analyst HAVING COUNT(*) >= 1
                   ORDER BY верных DESC NULLS LAST LIMIT 3''')
    аналитики = cur.fetchall()
    if аналитики:
        out.append("\n🎯 Точность TV аналитиков (проверено):")
        for а in аналитики:
            точность = (а[2] / а[1] * 100) if а[1] else 0
            out.append(f"  {а[0]}: {а[2]}/{а[1]} верных ({точность:.0f}%)")

    # Финальное решение
    out.append("\n🎯 Принятое решение:")
    if sol_сейчас > 75:
        out.append("  • Сработал бы СЛ — после нормализации перезапустить грид")
    elif sol_сейчас > 0 and sol_сейчас < 67:
        out.append("  • Сработал бы ТП — грид закрылся бы в плюс")
    else:
        ордеров_в_зоне = 0
        for о in ордера:
            if float(о.get('price', 0)) >= sol_сейчас:
                ордеров_в_зоне += 1
        out.append(f"  • Держать грид: {ордеров_в_зоне} ордеров в зоне fill")
        out.append("  • Ордера ждут роста SOL к $71-72 для открытия ШОРТА")
        out.append("  • Обоснование: рынок в медвежьем тренде, бэктест +4% по SHORT-мартингейлу")

    conn.close()
    print('\n'.join(out))


if __name__ == '__main__':
    main()
