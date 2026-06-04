#!/usr/bin/env python3
"""Monitor active grids and martingale chains. Compact report."""
import time, hmac, hashlib, requests, json, sys, os
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


def papi(p, b):
    t = str(sts())
    qs = json.dumps(b, separators=(',', ':'))
    h = {'X-BAPI-API-KEY': ak, 'X-BAPI-TIMESTAMP': t,
         'X-BAPI-SIGN': sreq(t, qs), 'X-BAPI-RECV-WINDOW': str(RW),
         'Content-Type': 'application/json'}
    return requests.post(f'{BASE}{p}', data=qs, headers=h, timeout=15).json()


def main():
    b = gapi('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})
    acct = b['result']['list'][0]
    avail = float(acct.get('totalAvailableBalance', 0))
    equity = float(acct.get('totalEquity', 0))

    p = gapi('/v5/position/list', {'category': 'linear', 'settleCoin': 'USDT'})
    pos_list = [pos for pos in p.get('result', {}).get('list', []) if float(pos.get('size', 0)) > 0]

    o = gapi('/v5/order/realtime', {'category': 'linear', 'settleCoin': 'USDT'})
    open_orders = [ord for ord in o.get('result', {}).get('list', []) if ord.get('orderStatus') in ('New', 'PartiallyFilled')]

    import psycopg2
    conn = psycopg2.connect(host='192.168.0.149', port=5432, database='cryptotrader',
                            user='cryptotrader', password='cryptotrader123')
    cur = conn.cursor()
    cur.execute('''SELECT grid_id, symbol, direction, total_budget, tp_price, sl_price
                   FROM grids WHERE status = 'active' ORDER BY created_at DESC''')
    grids = cur.fetchall()
    conn.close()

    print(f"💰 Баланс: ${avail:.2f} free / ${equity:.2f} eq")

    if pos_list:
        print("\n📍 Позиции:")
        for pp in pos_list:
            sym = pp['symbol'].replace('USDT', '')
            pnl = float(pp.get('unrealisedPnl', 0))
            entry = float(pp.get('entryPrice') or pp.get('avgPrice') or 0)
            mark = float(pp.get('markPrice', entry))
            side = 'L' if pp['side'] == 'Buy' else 'S'
            chg = ((mark - entry) / entry * 100) * (1 if side == 'L' else -1)
            print(f"  {side} {sym} qty={pp['size']} @ ${entry:.2f} → ${mark:.2f} ({chg:+.2f}%) pnl=${pnl:+.4f}")
    else:
        print("\n📍 Позиций нет")

    if open_orders:
        print("\n⏳ Ордера:")
        for oo in open_orders:
            sym = oo['symbol'].replace('USDT', '')
            print(f"  {oo['side']} {sym} {oo['qty']} @ ${oo['price']} | {oo.get('orderStatus', 'New')}")
    else:
        print("\n⏳ Нет активных ордеров")

    if grids:
        print("\n🔲 Активные сетки:")
        for g in grids:
            print(f"  {g[0]} | {g[1]} {g[2]} | budget=${float(g[3]):.2f} | TP=${g[4]} SL=${g[5]}")


if __name__ == '__main__':
    main()
