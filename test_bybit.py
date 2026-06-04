from src.gateways.bybit_api import BybitAPI
api = BybitAPI("TjIJ9Gm5IY3PlNiXon", "8D5Da8ZxPMgDdiLqEVqtWaMoxpT3Jb4D8Ji0", testnet=False)
print("Testing Bybit...")
print(api.get_wallet_balance("UNIFIED"))