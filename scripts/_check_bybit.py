import sys
sys.path.insert(0, '/home/andy')
from src.gateways.bybit_api import BybitAPI
from src.core.config import Config

config = Config()
api = BybitAPI(
    config.bybit['api_key'],
    config.bybit['api_secret'],
    testnet=config.bybit.get('testnet', False)
)
positions = api.get_positions(category='linear')
print('Bybit positions:', positions)
