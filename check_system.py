"""Quick system check"""
from src.core.config import Config

c = Config.load()
print("Config loaded OK")
print(f"Binance testnet: {c.binance.get('testnet')}")
print(f"API key prefix: {c.binance.get('api_key', 'MISSING')[:10]}...")
print(f"PostgreSQL host: {c.postgresql.get('host')}")
print(f"OpenRouter model: {c.openrouter.get('model')}")
print(f"RAGFlow configured: {bool(c.ragflow.get('api_key'))}")
print(f"Telegram enabled: {c.telegram.get('enabled')}")
