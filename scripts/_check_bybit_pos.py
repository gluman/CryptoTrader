import sys
sys.path.insert(0, '/home/andy/cryptotrader')

from src.gateways.bybit_api import BybitAPI
from src.core.config import Config

config = Config()
api = BybitAPI(config.bybit_api_key, config.bybit_secret, testnet=config.bybit_testnet)
positions = api.get_positions()
print('Bybit positions:', positions)
