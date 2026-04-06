"""System health check"""
import sys
sys.path.insert(0, '.')

from src.core.config import Config
from src.core.database import DatabaseManager
from src.core.logger import setup_logger
from src.gateways import BinanceAPI, RAGFlowAPI

config = Config.load()
logger = setup_logger('check', level='INFO')

print("=" * 60)
print("CRYPTO TRADER - SYSTEM HEALTH CHECK")
print("=" * 60)

# 1. Database
print("\n[1/4] PostgreSQL...")
try:
    db = DatabaseManager(config.postgresql, logger)
    if db.test_connection():
        print("  ✅ Connected to PostgreSQL")
        db.create_tables()
        print("  ✅ Tables created/verified")
    else:
        print("  ❌ Connection failed")
except Exception as e:
    print(f"  ❌ Error: {e}")

# 2. Binance
print("\n[2/4] Binance API...")
try:
    binance = BinanceAPI(
        api_key=config.binance['api_key'],
        api_secret=config.binance['api_secret'],
        testnet=config.binance.get('testnet', False),
        logger=logger
    )
    account = binance.get_account()
    # Get USDT balance
    for b in account.get('balances', []):
        if b['asset'] == 'USDT':
            free = float(b['free'])
            locked = float(b['locked'])
            print(f"  ✅ Connected to Binance")
            print(f"  💰 USDT Balance: {free:.2f} (free) / {locked:.2f} (locked)")
            break
    print(f"  🌐 Mode: {'TESTNET' if config.binance.get('testnet') else 'MAINNET'}")
except Exception as e:
    print(f"  ❌ Error: {e}")

# 3. OpenRouter
print("\n[3/4] OpenRouter API...")
try:
    import requests
    headers = {'Authorization': f'Bearer {config.openrouter["api_key"]}'}
    resp = requests.get('https://openrouter.ai/api/v1/auth/key', headers=headers, timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        print(f"  ✅ OpenRouter connected")
        print(f"  🤖 Model: {config.openrouter['model']}")
    else:
        print(f"  ⚠️ Response: {resp.status_code}")
except Exception as e:
    print(f"  ❌ Error: {e}")

# 4. RAGFlow
print("\n[4/4] RAGFlow...")
try:
    ragflow = RAGFlowAPI(
        base_url=config.ragflow.get('base_url', ''),
        api_key=config.ragflow.get('api_key', ''),
        logger=logger
    )
    datasets = ragflow.list_datasets()
    print(f"  ✅ RAGFlow connected ({len(datasets)} datasets)")
except Exception as e:
    print(f"  ⚠️ RAGFlow: {e}")

print("\n" + "=" * 60)
print("HEALTH CHECK COMPLETE")
print("=" * 60)
