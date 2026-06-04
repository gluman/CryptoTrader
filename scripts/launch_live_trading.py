#!/usr/bin/env python3
"""Launch grid/martingale live trading with time-sync fix"""
import sys, os, time, json, requests, hmac, hashlib
sys.path.insert(0, '/home/andy/cryptotrader')

# Load keys from .env
API_KEY = ''
API_SECRET = ''
with open('/home/andy/.env') as f:
    for line in f:
        line = line.strip()
        if '=' not in line or line.startswith('#'):
            continue
        k, v = line.split('=', 1)
        if k == 'BYBIT_API_KEY':
            API_KEY = v
        elif k == 'BYBIT_API_SECRET':
            API_SECRET = v

RECV_WINDOW = 60000
BASE = 'https://api.bybit.com'

def get_server_time():
    resp = requests.get(f'{BASE}/v5/market/time', timeout=10)
    data = resp.json()
    return int(data['result']['timeSecond']) * 1000

def signed_get(endpoint, params=None):
    server_ts = get_server_time()
    local_ts = int(time.time() * 1000)
    # Use server time to avoid drift
    ts = str(server_ts)
    
    if params:
        param_str = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    else:
        param_str = ''
    
    sign_str = f"{ts}{API_KEY}{RECV_WINDOW}{param_str}"
    sig = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        'X-BAPI-API-KEY': API_KEY,
        'X-BAPI-TIMESTAMP': ts,
        'X-BAPI-SIGN': sig,
        'X-BAPI-RECV-WINDOW': str(RECV_WINDOW),
    }
    
    resp = requests.get(f'{BASE}{endpoint}', params=params, headers=headers, timeout=15)
    return resp.json()

def signed_post(endpoint, body):
    server_ts = get_server_time()
    ts = str(server_ts)
    param_str = json.dumps(body, separators=(',', ':'))
    
    sign_str = f"{ts}{API_KEY}{RECV_WINDOW}{param_str}"
    sig = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        'X-BAPI-API-KEY': API_KEY,
        'X-BAPI-TIMESTAMP': ts,
        'X-BAPI-SIGN': sig,
        'X-BAPI-RECV-WINDOW': str(RECV_WINDOW),
        'Content-Type': 'application/json',
    }
    
    resp = requests.post(f'{BASE}{endpoint}', data=param_str, headers=headers, timeout=15)
    return resp.json()

# 1. Check balance
print("=== CHECKING BALANCE ===")
data = signed_get('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})
if data.get('retCode') != 0:
    print(f"ERROR: {data.get('retMsg')}")
    sys.exit(1)

result = data.get('result', {})
avail = float(result.get('totalAvailableBalance', 0))
equity = float(result.get('totalEquity', 0))
print(f"Available: ${avail:.4f} USDT")
print(f"Equity: ${equity:.4f}")

# 2. Check open positions
print("\n=== OPEN POSITIONS ===")
positions = signed_get('/v5/position/list', {'category': 'linear', 'settleCoin': 'USDT'})
pos_list = positions.get('result', {}).get('list', [])
print(f"Open positions: {len(pos_list)}")
for p in pos_list:
    print(f"  {p['symbol']} {p['side']} qty={p['size']} entry={p['entryPrice']} pnl={p.get('unrealisedPnl', '0')}")

# 3. Get SOL price
print("\n=== SOL PRICE ===")
ticker = requests.get(f'{BASE}/v5/market/tickers', params={'category': 'linear', 'symbol': 'SOLUSDT'}, timeout=10).json()
sol_price = float(ticker['result']['list'][0]['lastPrice'])
print(f"SOL/USDT: ${sol_price}")

# 4. Calculate and place martingale SHORT on SOLUSDT
budget = min(10.0, avail * 0.30)
print(f"\n=== PLACING MARTINGALE SHORT SOL ===")
print(f"Budget: ${budget:.2f}")

splits = [0.22, 0.33, 0.45]
step_pct = 2.5
orders = []

for step in range(3):
    price = round(sol_price * (1 + step_pct / 100 * (step + 1)), 2)
    amount_usdt = round(budget * splits[step], 2)
    # For linear: qty in SOL
    qty = round(amount_usdt / price, 1)  # SOL has 1 decimal
    
    print(f"  Step {step+1}: SELL {qty} SOL @ ${price} (${amount_usdt:.2f})")
    orders.append({'step': step+1, 'price': price, 'qty': qty, 'usdt': amount_usdt})

# Set leverage first
print("\nSetting leverage 3x...")
lev_resp = signed_post('/v5/position/set-leverage', {
    'category': 'linear',
    'symbol': 'SOLUSDT',
    'buyLeverage': '3',
    'sellLeverage': '3',
})
print(f"Leverage: {lev_resp.get('retMsg')}")

# Place Step 1 order
step1 = orders[0]
print(f"\nPlacing Step 1: SELL {step1['qty']} @ ${step1['price']}...")
order_resp = signed_post('/v5/order/create', {
    'category': 'linear',
    'symbol': 'SOLUSDT',
    'side': 'Sell',
    'orderType': 'Limit',
    'qty': str(step1['qty']),
    'price': str(step1['price']),
    'timeInForce': 'GTC',
})

if order_resp.get('retCode') == 0:
    order_id = order_resp['result']['orderId']
    print(f"✅ Step 1 placed! Order ID: {order_id}")
else:
    print(f"❌ Step 1 failed: {order_resp.get('retMsg')}")

# Place Step 2 order
step2 = orders[1]
print(f"\nPlacing Step 2: SELL {step2['qty']} @ ${step2['price']}...")
order_resp2 = signed_post('/v5/order/create', {
    'category': 'linear',
    'symbol': 'SOLUSDT',
    'side': 'Sell',
    'orderType': 'Limit',
    'qty': str(step2['qty']),
    'price': str(step2['price']),
    'timeInForce': 'GTC',
})

if order_resp2.get('retCode') == 0:
    order_id2 = order_resp2['result']['orderId']
    print(f"✅ Step 2 placed! Order ID: {order_id2}")
else:
    print(f"❌ Step 2 failed: {order_resp2.get('retMsg')}")

# Place Step 3 order
step3 = orders[2]
print(f"\nPlacing Step 3: SELL {step3['qty']} @ ${step3['price']}...")
order_resp3 = signed_post('/v5/order/create', {
    'category': 'linear',
    'symbol': 'SOLUSDT',
    'side': 'Sell',
    'orderType': 'Limit',
    'qty': str(step3['qty']),
    'price': str(step3['price']),
    'timeInForce': 'GTC',
})

if order_resp3.get('retCode') == 0:
    order_id3 = order_resp3['result']['orderId']
    print(f"✅ Step 3 placed! Order ID: {order_id3}")
else:
    print(f"❌ Step 3 failed: {order_resp3.get('retMsg')}")

# Save to DB
import psycopg2
conn = psycopg2.connect(host='192.168.0.149', port=5432, database='cryptotrader',
                        user='cryptotrader', password='cryptotrader123')
cur = conn.cursor()

# Create tables if needed
cur.execute("""
    CREATE TABLE IF NOT EXISTS grids (
        grid_id VARCHAR(36) PRIMARY KEY,
        symbol VARCHAR(20) NOT NULL,
        market_type VARCHAR(10) NOT NULL,
        direction VARCHAR(10) NOT NULL,
        config_json JSONB NOT NULL,
        tp_price NUMERIC, sl_price NUMERIC,
        total_budget NUMERIC,
        status VARCHAR(10) DEFAULT 'active',
        realized_pnl NUMERIC DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        closed_at TIMESTAMPTZ
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS grid_orders (
        id SERIAL PRIMARY KEY,
        grid_id VARCHAR(36) REFERENCES grids(grid_id),
        step INT NOT NULL,
        side VARCHAR(10) NOT NULL,
        price NUMERIC NOT NULL,
        amount NUMERIC NOT NULL,
        amount_usdt NUMERIC NOT NULL,
        order_id VARCHAR(36) DEFAULT '',
        status VARCHAR(10) DEFAULT 'pending',
        filled_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
""")

grid_id = f"mart_short_sol_{int(time.time())}"
import json as jsonlib
cur.execute("""
    INSERT INTO grids (grid_id, symbol, market_type, direction, config_json, tp_price, sl_price, total_budget)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
""", (grid_id, 'SOLUSDT', 'linear', 'short',
      jsonlib.dumps(orders),
      round(sol_price * 0.975, 2),   # TP at -2.5%
      round(sol_price * 1.10, 2),    # SL at +10%
      budget))

for o in orders:
    oid = ''
    if o['step'] == 1 and order_resp.get('retCode') == 0:
        oid = order_resp['result']['orderId']
    elif o['step'] == 2 and order_resp2.get('retCode') == 0:
        oid = order_resp2['result']['orderId']
    elif o['step'] == 3 and order_resp3.get('retCode') == 0:
        oid = order_resp3['result']['orderId']
    
    cur.execute("""
        INSERT INTO grid_orders (grid_id, step, side, price, amount, amount_usdt, order_id, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (grid_id, o['step'], 'Sell', o['price'], o['qty'], o['usdt'],
          oid, 'placed' if oid else 'failed'))

conn.commit()
conn.close()

print(f"\n✅ Grid saved to DB: {grid_id}")
print(f"\n=== SUMMARY ===")
print(f"Symbol: SOLUSDT")
print(f"Direction: SHORT (martingale 3-step)")
print(f"Budget: ${budget:.2f}")
print(f"Step 1: SELL {step1['qty']} @ ${step1['price']} ({'placed' if order_resp.get('retCode')==0 else 'FAILED'})")
print(f"Step 2: SELL {step2['qty']} @ ${step2['price']} ({'placed' if order_resp2.get('retCode')==0 else 'FAILED'})")
print(f"Step 3: SELL {step3['qty']} @ ${step3['price']} ({'placed' if order_resp3.get('retCode')==0 else 'FAILED'})")
print(f"TP: ${round(sol_price * 0.975, 2)} | SL: ${round(sol_price * 1.10, 2)}")
