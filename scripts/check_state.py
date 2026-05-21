#!/usr/bin/env python3
import sys, os
sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')
from src.gateways.bybit_api import BybitAPI
from src.core.config import Config

cfg = Config.load()
api = BybitAPI(cfg.bybit['api_key'], cfg.bybit['api_secret'], testnet=False)

# Balance
bal = api.get_wallet_balance('UNIFIED')
coin = next((c for c in bal['result']['list'][0]['coin'] if c['coin'] == 'USDT'), None)
if coin:
    print(f"USDT Balance: {coin['walletBalance']}")
    print(f"Available: {bal['result']['list'][0]['totalAvailableBalance']}")

# Positions
pos = api.get_positions(category='linear')
plist = pos['result']['list']
print(f"Open positions: {len(plist)}")
for p in plist:
    print(f"  {p['symbol']}: size={p['size']}, entry={p['avgPrice']}, pnl={p['unrealisedPnl']}, sl={p['stopLoss']}, tp={p['takeProfit']}")
