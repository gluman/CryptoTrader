#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')
from src.gateways import BinanceAPI, BybitAPI, BitfinexAPI, CoinExAPI
import logging

logging.basicConfig(level=logging.INFO)

def test_binance():
    print("=== Binance Test ===")
    try:
        b = BinanceAPI(
            "3UnB7jzdOA8Q31kkygFva8OKHthB6v8ajK7zALS47ww4FgkuTc12cfXMCKomnrTQ",
            "Pa5V5l6r0ZK75d6WQna1C0y0dtUdDNzlTVyOSmfuvlcXMIx2Z52kwsrSo1vZLVn7",
            testnet=False,
            logger=logging.getLogger()
        )
        # Test public endpoint first
        ticker = b.get_ticker('BTCUSDT')
        print(f"BTCUSDT Price: ${float(ticker['lastPrice']):,.2f}")
        
        # Try to get balance
        try:
            account = b.get_account()
            balances = b.get_balances()
            print(f"Account assets: {len(balances)}")
            total_usdt = 0
            for bal in balances:
                asset = bal['asset']
                free = float(bal['free'])
                locked = float(bal['locked'])
                if free > 0 or locked > 0:
                    print(f"  {asset}: Free={free:.8f}, Locked={locked:.8f}")
                    if asset == 'USDT':
                        total_usdt += free
            print(f"USDT Balance: ${total_usdt:.2f}")
            return total_usdt
        except Exception as e:
            print(f"Balance error: {e}")
            return 0
    except Exception as e:
        print(f"Binance error: {e}")
        return 0

def test_bybit():
    print("\n=== Bybit Test ===")
    try:
        b = BybitAPI("TjIJ9Gm5IY3PlNiXon", "8D5Da8ZxPMgDdiLqEVqtWaMoxpT3Jb4D8Ji0", testnet=False)
        # Test public endpoint
        ticker = b.get_tickers(symbol='BTCUSDT')
        print(f"BTCUSDT Price: ${float(ticker['result']['list'][0]['lastPrice']):,.2f}")
        
        # Try to get balance
        try:
            balance = b.get_wallet_balance('UNIFIED')
            wallets = balance['result']['list'][0]['coin']
            total_usdt = 0
            for coin in wallets:
                if float(coin['available']) > 0 or float(coin['locked']) > 0:
                    print(f"  {coin['coin']}: Available={coin['available']}, Locked={coin['locked']}")
                    if coin['coin'] == 'USDT':
                        total_usdt += float(coin['available'])
            print(f"USDT Balance: ${total_usdt:.2f}")
            return total_usdt
        except Exception as e:
            print(f"Balance error: {e}")
            return 0
    except Exception as e:
        print(f"Bybit error: {e}")
        return 0

def test_bitfinex():
    print("\n=== Bitfinex Test ===")
    try:
        b = BitfinexAPI("2adab7c5c61637d82e8c49b9e8c1b2caa74402e0c77", "88d5c347d5e4773ad2e25fb6202fba8cd2ecaadfedc", testnet=False)
        # Test public endpoint
        ticker = b.get_ticker('tBTCUSD')
        print(f"BTCUSD Price: ${float(ticker[6]):,.2f}")
        
        # Try to get balance
        try:
            balances = b.get_balances()
            total_usd = 0
            for bal in balances:
                currency = bal[1]
                balance = float(bal[2])
                if balance > 0:
                    print(f"  {currency}: {balance:.8f}")
                    if currency == 'UST':  # USDT on Bitfinex
                        total_usd += balance
            print(f"USD Balance: ${total_usd:.2f}")
            return total_usd
        except Exception as e:
            print(f"Balance error: {e}")
            return 0
    except Exception as e:
        print(f"Bitfinex error: {e}")
        return 0

def test_coinex():
    print("\n=== CoinEx Test ===")
    try:
        b = CoinExAPI("B668BEF137964B32A8B364F26B5E8A3E", "FDBE5CEA092CC27B43034D3BA3E4A3054C654AEE94219A22", testnet=False)
        # Test public endpoint
        ticker = b.get_ticker('BTCUSDT')
        print(f"BTCUSDT Price: ${float(ticker['last_price']):,.2f}")
        
        # Try to get balance
        try:
            balance = b.get_balance()
            balances = b.get_balances_list()
            total_usdt = 0
            for bal in balances:
                asset = bal['asset']
                available = float(bal['available'])
                frozen = float(bal['frozen'])
                if available > 0 or frozen > 0:
                    print(f"  {asset}: Available={available:.8f}, Frozen={frozen:.8f}")
                    if asset == 'USDT':
                        total_usdt += available
            print(f"USDT Balance: ${total_usdt:.2f}")
            return total_usdt
        except Exception as e:
            print(f"Balance error: {e}")
            return 0
    except Exception as e:
        print(f"CoinEx error: {e}")
        return 0

if __name__ == "__main__":
    binance_balance = test_binance()
    bybit_balance = test_bybit()
    bitfinex_balance = test_bitfinex()
    coinex_balance = test_coinex()
    
    print(f"\n=== Summary ===")
    print(f"Binance USDT: ${binance_balance:.2f}")
    print(f"Bybit USDT: ${bybit_balance:.2f}")
    print(f"Bitfinex USDT: ${bitfinex_balance:.2f}")
    print(f"CoinEx USDT: ${coinex_balance:.2f}")
    
    total_balance = binance_balance + bybit_balance + bitfinex_balance + coinex_balance
    print(f"\nTotal USDT across all exchanges: ${total_balance:.2f}")
    
    if total_balance >= 1000:
        print("✓ Trading capability: EXCELLENT (Sufficient funds for substantial trading)")
    elif total_balance >= 100:
        print("✓ Trading capability: GOOD (Sufficient funds for trading)")
    elif total_balance >= 10:
        print("⚠ Trading capability: LIMITED (Minimal funds for small trades)")
    else:
        print("✗ Trading capability: INSUFFICIENT (Insufficient funds for meaningful trading)")