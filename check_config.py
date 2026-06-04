from src.core.config import Config

config = Config.load()
print(f"API Key from config: {config.ragflow.get('api_key')}")
print(f"Base URL: {config.ragflow.get('base_url')}")
print(f"Dataset ID: {config.ragflow.get('dataset_id')}")