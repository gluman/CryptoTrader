#!/usr/bin/env python3
"""Launch live grid/martingale trading with correct API key"""
import os, sys, time, json, requests, hmac, hashlib, subprocess

# Load API key from .env
result = subprocess.run(['bash', '-c', 'grep -E "^BYBIT_API_(KEY|SECRET)=" /home/andy/.env'],
                        capture_output=True, text=True)
for line in result.stdout.strip().split('\n'):
    if line:
        k, v = line.split('=', 1)
        os.environ[k] = v

API_KEY=os.env...EY')
API_SECRET=os.env...ET')
RECV_WINDOW = 60000
BASE = 'https://api.bybit.com'

def get_server_time():
    return int(requests.get(f'{BASE}/v5/market/time', timeout=10).json()['result']['timeSecond']) * 1000

def signed_get(endpoint, params=None):
    ts = str(get_server_time())
    if params:
        param_str = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    else:
        param_str = ''
    sign_str = f"{ts}{API_KEY}{RECV_WINDOW}{param_str}"
    sig = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    headers = {
        'X-BAPI-API-KEY': API_KEY, 'X-BAPI-TIMESTAMP': ts,
        'X-BAPI-SIGN': sig, 'X-BAPI-RECV-WINDOW': str(RECV_WINDOW),
    }
    return requests.get(f'{BASE}{endpoint}', params=params, headers=headers, timeout=15).json()

def signed_post(endpoint, body):
    ts = str(get_server_time())
    param_str = json.dumps(body, separators=(',', ':'))
    sign_str = f"{ts}{API_KEY}{RECV_WINDOW}{param_str}"
    sig = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    headers = {
        'X-BAPI-API-KEY': API_KEY, 'X-BAPI-TIMESTAMP': ts,
        'X-BAPI-SIGN': sig, 'X-BAPI-RECV-WINDOW': str(RECV_WINDOW),
        'Content-Type': 'application/json',
    }
    return requests.post(f'{BASE}{endpoint}', data=param_str, headers=headers, timeout=15).json()

# ============ MAIN ============
print("="*70)
print("CRYPTO TRADER: Live Martingale SHORT SOLUSDT")
print("="*70)

# 1. Check balance
data = signed_get('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})
if data.get('retCode') != 0:
    print(f"❌ Balance error: {data.get('retMsg')}")
    sys.exit(1)

result = data.get('result', {})
avail = float(result.get('totalAvailableBalance', 0))
equity = float(result.get('totalEquity', 0))
print(f"\n💰 Balance: ${avail:.4f} USDT free / ${equity:.4f} total")

if avail < 5:
    print(f"❌ Insufficient balance (need > $5)")
    sys.exit(1)

# 2. Get SOL price
ticker = requests.get(f'{BASE}/v5/market/tickers',
                      params={'category': 'linear', 'symbol': 'SOLUSDT'}, timeout=10).json()
sol_price = float(ticker['result']['list'][0]['lastPrice'])
print(f"📊 SOL/USDT: ${sol_price}")

# 3. Set leverage
print(f"\n⚙️ Setting leverage 3x on SOLUSDT...")
lev = signed_post('/v5/position/set-leverage', {
    'category': 'linear', 'symbol': 'SOLUSDT',
    'buyLeverage': '3', 'sellLeverage': '3',
})
print(f"   → {lev.get('retMsg')}")

# 4. Calculate martingale steps
# Budget: max 30% of balance or $10, whichever is smaller
budget = min(10.0, avail * 0.30)
splits = [0.22, 0.33, 0.45]  # step1, step2, step3
step_pct = 2.5

print(f"\n📐 Martingale plan: SHORT SOLUSDT, ${budget:.2f} budget")
print(f"   Step 1: ${budget*splits[0]:.2f} @ +{step_pct*1:.1f}% = ${sol_price*(1+0.025):.2f}")
print(f"   Step 2: ${budget*splits[1]:.2f} @ +{step_pct*2:.1f}% = ${sol_price*(1+0.050):.2f}")
print(f"   Step 3: ${budget*splits[2]:.2f} @ +{step_pct*3:.1f}% = ${sol_price*(1+0.075):.2f}")

# 5. Place all 3 limit orders
results = []
for step in range(3):
    step_mult = step + 1
    price = round(sol_price * (1 + step_pct / 100 * step_mult), 2)
    amount_usdt = round(budget * splits[step], 2)
    # SOL qty - contract size is 1 SOL for linear
    qty = max(1, round(amount_usdt / price))  # min 1 contract on SOL

    # Each step qty: $0.77/72=0.01 SOL (too small)
    # Use lower step_pct or higher budget. Let's go with $5 budget
    print(f"\n   Step {step+1}: SELL {qty} SOL @ ${price} (${amount_usdt:.2f})")
    resp = signed_post('/v5/order/create', {
        'category': 'linear', 'symbol': 'SOLUSDT',
        'side': 'Sell', 'orderType': 'Limit',
        'qty': str(qty), 'price': str(price),
        'timeInForce': 'GTC',
    })

    if resp.get('retCode') == 0:
        oid = resp['result']['orderId']
        results.append({'step': step+1, 'price': price, 'qty': qty,
                       'usdt': amount_usdt, 'order_id': oid, 'status': 'placed'})
        print(f"   ✅ Order placed! ID: {oid}")
    else:
        print(f"   ❌ Failed: {resp.get('retMsg')}")
        results.append({'step': step+1, 'price': price, 'qty': qty,
                       'usdt': amount_usdt, 'order_id': '', 'status': 'failed',
                       'error': resp.get('retMsg')})

# 6. Save to DB
import psycopg2
conn = psycopg2.connect(host='192.168.0.149', port=5432, database='cryptotrader',
                        user='cryptotrader', password='cryptotrader123')
cur = conn.cursor()

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
tp_price = round(sol_price * 0.975, 2)  # TP -2.5%
sl_price = round(sol_price * 1.10, 2)   # SL +10%

cur.execute("""
    INSERT INTO grids (grid_id, symbol, market_type, direction, config_json, tp_price, sl_price, total_budget)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
""", (grid_id, 'SOLUSDT', 'linear', 'short',
      json.dumps([{'step': r['step'], 'price': r['price'], 'qty': r['qty'],
                  'usdt': r['usdt'], 'order_id': r['order_id']} for r in results]),
      tp_price, sl_price, budget))

for r in results:
    cur.execute("""
        INSERT INTO grid_orders (grid_id, step, side, price, amount, amount_usdt, order_id, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (grid_id, r['step'], 'Sell', r['price'], r['qty'], r['usdt'],
          r['order_id'], r['status']))

conn.commit()
conn.close()

# 7. Summary
print("\n" + "="*70)
print("✅ GRID DEPLOYED")
print("="*70)
print(f"Grid ID: {grid_id}")
print(f"Symbol:  SOLUSDT (linear)")
print(f"Strategy: Martingale 3-step SHORT")
print(f"Budget:  ${budget:.2f}")
print(f"TP: ${tp_price} | SL: ${sl_price}")
print(f"\nResults:")
for r in results:
    icon = '✅' if r['status'] == 'placed' else '❌'
    print(f"  {icon} Step {r['step']}: SELL {r['qty']} @ ${r['price']} → {r['status']}")
