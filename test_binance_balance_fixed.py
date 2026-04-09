#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')
from src.gateways import BinanceAPI
import logging
import time

logging.basicConfig(level=logging.INFO)
b = BinanceAPI(
    "3UnB7jzdOA8Q31kkygFva8OKHthB6v8ajK7zALS47ww4FgkuTc12cfXMCKomnrTQ",
    "Pa5V5l6r0ZK75d6WQna1C0y0dtUdDNzlTVyOSmfuvlcXMIx2Z52kwsrSo1vZLVn7",
    testnet=False,
    logger=logging.getLogger()
)
print("=== Binance Balance Test ===")
try:
    # First check clock sync
    print("Checking clock sync...")
    if b.check_clock_sync():
        print("Clock sync: OK")
    else:
        print("Clock sync: ISSUE DETECTED")
        time.sleep(1)  # Wait a second to sync
    
    # Get account info
    print("\nGetting account balance...")
    account = b.get_account()
    print(f"Account status: {account.get('accountType', 'N/A')}")
    print(f"Maker commission: {account.get('makerCommission', 'N/A')}%")
    print(f"Taker commission: {account.get('takerCommission', 'N/A')}%")
    
    balances = b.get_balances()
    print(f"\nTotal balance assets: {len(balances)}")
    total_usdt_value = 0
    
    print("\nNon-zero balances:")
    for balance in balances:
        asset = balance['asset']
        free = float(balance['free'])
        locked = float(balance['locked'])
        total = free + locked
        
        if total > 0:
            print(f"  {asset}: Free={free:.8f}, Locked={locked:.8f}, Total={total:.8f}")
            # Estimate USDT value (rough conversion)
            if asset == 'USDT':
                total_usdt_value += total
            elif asset == 'BTC':
                # Get BTC price
                try:
                    btc_ticker = b.get_ticker('BTCUSDT')
                    btc_price = float(btc_ticker['lastPrice'])
                    total_usdt_value += total * btc_price
                except Exception as e:
                    print(f"    Error getting BTC price: {e}")
            elif asset == 'ETH':
                # Get ETH price
                try:
                    eth_ticker = b.get_ticker('ETHUSDT')
                    eth_price = float(eth_ticker['lastPrice'])
                    total_usdt_value += total * eth_price
                except Exception as e:
                    print(f"    Error getting ETH price: {e}")
    
    print(f"\nEstimated total USDT value: ${total_usdt_value:.2f}")
    
    # Check trading capability
    if total_usdt_value >= 100:  # Minimum $100 for meaningful trading
        print("✓ Trading capability: GOOD (Sufficient funds)")
    elif total_usdt_value >= 10:  # Minimum $10 for small trades
        print("⚠ Trading capability: LIMITED (Minimal funds)")
    else:
        print("✗ Trading capability: INSUFFICIENT (Insufficient funds)")
    
    print("=== Binance Balance Test Complete ===")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()