#!/usr/bin/env python3
"""Live grid/martingale trader. Keys loaded from BKEY/BSEC env vars."""
import os, sys, time, json, requests, hmac, hashlib

API_KEY=*** not in os.environ or 'BSEC' not in os.environ:
    print('Set BKEY and BSEC env vars from /home/andy/.env')
    sys.exit(1)
API_KEY=***SECRET=***= 60000
BASE = 'https://api.bybit.com'

def server_ts():
    return int(requests.get(f'{BASE}/v5/market/time', timeout=10).json()['result']['timeSecond']) * 1000

def sign_req(ts, param_str):
    s = f"{ts}{API_KEY}{RECV_WINDOW}{param_str}"
    return hmac.new(API_SECRET.encode(), s.encode(), hashlib.sha256).hexdigest()

def get_api(path, params=None):
    ts = str(server_ts())
    if params:
        ps = '&'.join(f"{k}={v}" for k, v in sorted(params.items()))
    else:
        ps = ''
    h = {'X-BAPI-API-KEY': API_KEY, 'X-BAPI-TIMESTAMP': ts,
         'X-BAPI-SIGN': sign_req(ts, ps), 'X-BAPI-RECV-WINDOW': str(RECV_WINDOW)}
    return requests.get(f'{BASE}{path}', params=params, headers=h, timeout=15).json()

def post_api(path, body):
    ts = str(server_ts())
    ps = json.dumps(body, separators=(',', ':'))
    h = {'X-BAPI-API-KEY': API_KEY, 'X-BAPI-TIMESTAMP': ts,
         'X-BAPI-SIGN': sign_req(ts, ps), 'X-BAPI-RECV-WINDOW': str(RECV_WINDOW),
         'Content-Type': 'application/json'}
    return requests.post(f'{BASE}{path}', data=ps, headers=h, timeout=15).json()

print('=' * 70)
print('CRYPTO TRADER: Live Martingale SHORT SOLUSDT')
print('=' * 70)

d = get_api('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})
if d.get('retCode') != 0:
    print(f"ERR Balance: {d.get('retMsg')}")
    sys.exit(1)
res = d['result']
avail = float(res.get('totalAvailableBalance', 0))
equity = float(res.get('totalEquity', 0))
print(f"\nBalance: ${avail:.4f} free / ${equity:.4f} total")

if avail < 5:
    print("ERR balance < $5")
    sys.exit(1)

t = requests.get(f'{BASE}/v5/market/tickers',
                 params={'category': 'linear', 'symbol': 'SOLUSDT'}, timeout=10).json()
sol_price = float(t['result']['list'][0]['lastPrice'])
print(f"SOL/USDT: ${sol_price}")

print("\nSetting leverage 3x...")
lv = post_api('/v5/position/set-leverage', {
    'category': 'linear', 'symbol': 'SOLUSDT',
    'buyLeverage': '3', 'sellLeverage': '3',
})
print(f"   -> {lv.get('retMsg')}")

budget = min(10.0, avail * 0.30)
splits = [0.22, 0.33, 0.45]
step_pct = 2.5

print(f"\nPlan: SHORT SOL ${budget:.2f}, step {step_pct}%")
for i in range(3):
    p = round(sol_price * (1 + step_pct / 100 * (i + 1)), 2)
    a = round(budget * splits[i], 2)
    print(f"  Step {i+1}: ${a:.2f} @ ${p}")

results = []
for step in range(3):
    price = round(sol_price * (1 + step_pct / 100 * (step + 1)), 2)
    amount_usdt = round(budget * splits[step], 2)
    qty = max(1, round(amount_usdt / price))
    print(f"\n  Step {step+1}: SELL {qty} SOL @ ${price}")
    r = post_api('/v5/order/create', {
        'category': 'linear', 'symbol': 'SOLUSDT',
        'side': 'Sell', 'orderType': 'Limit',
        'qty': str(qty), 'price': str(price),
        'timeInForce': 'GTC',
    })
    if r.get('retCode') == 0:
        oid = r['result']['orderId']
        results.append({'step': step+1, 'price': price, 'qty': qty,
                       'usdt': amount_usdt, 'order_id': oid, 'status': 'placed'})
        print(f"  OK id={oid}")
    else:
        print(f"  FAIL: {r.get('retMsg')}")
        results.append({'step': step+1, 'price': price, 'qty': qty,
                       'usdt': amount_usdt, 'order_id': '', 'status': 'failed'})

import psycopg2
conn = psycopg2.connect(host='192.168.0.149', port=5432, database='cryptotrader',
                        user='cryptotrader', password='cryptotrader123')
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS grids (grid_id VARCHAR(36) PRIMARY KEY, symbol VARCHAR(20) NOT NULL, market_type VARCHAR(10) NOT NULL, direction VARCHAR(10) NOT NULL, config_json JSONB NOT NULL, tp_price NUMERIC, sl_price NUMERIC, total_budget NUMERIC, status VARCHAR(10) DEFAULT 'active', realized_pnl NUMERIC DEFAULT 0, created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(), closed_at TIMESTAMPTZ)")
cur.execute("CREATE TABLE IF NOT EXISTS grid_orders (id SERIAL PRIMARY KEY, grid_id VARCHAR(36) REFERENCES grids(grid_id), step INT NOT NULL, side VARCHAR(10) NOT NULL, price NUMERIC NOT NULL, amount NUMERIC NOT NULL, amount_usdt NUMERIC NOT NULL, order_id VARCHAR(36) DEFAULT '', status VARCHAR(10) DEFAULT 'pending', filled_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW())")

grid_id = f"mart_short_sol_{int(time.time())}"
tp = round(sol_price * 0.975, 2)
sl = round(sol_price * 1.10, 2)

cur.execute("INSERT INTO grids (grid_id, symbol, market_type, direction, config_json, tp_price, sl_price, total_budget) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (grid_id, 'SOLUSDT', 'linear', 'short',
             json.dumps([{'step': r['step'], 'price': r['price'], 'qty': r['qty'],
                         'usdt': r['usdt'], 'order_id': r['order_id']} for r in results]),
             tp, sl, budget))

for r in results:
    cur.execute("INSERT INTO grid_orders (grid_id, step, side, price, amount, amount_usdt, order_id, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (grid_id, r['step'], 'Sell', r['price'], r['qty'], r['usdt'], r['order_id'], r['status']))

conn.commit()
conn.close()

print('\n' + '=' * 70)
print('GRID DEPLOYED')
print('=' * 70)
print(f"id={grid_id}")
print(f"TP=${tp} SL=${sl}")
for r in results:
    print(f"  Step {r['step']}: {r['status']} qty={r['qty']} @ ${r['price']}")
