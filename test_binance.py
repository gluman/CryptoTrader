#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')
from src.gateways import BinanceAPI
import logging

logging.basicConfig(level=logging.INFO)
b = BinanceAPI(
    "3UnB7jzdOA8Q31kkygFva8OKHthB6v8ajK7zALS47ww4FgkuTc12cfXMCKomnrTQ",
    "Pa5V5l6r0ZK75d6WQna1C0y0dtUdDNzlTVyOSmfuvlcXMIx2Z52kwsrSo1vZLVn7",
    testnet=False,
    logger=logging.getLogger()
)
tickers = b.get_all_tickers()
print(f"Tickers count: {len(tickers)}")
if tickers:
    print(f"First: {tickers[0]}")