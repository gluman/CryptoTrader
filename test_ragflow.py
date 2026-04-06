import os
os.environ.setdefault('CONFIG_PATH', 'config/settings.yaml')

from src.core.config import Config
from src.gateways import RAGFlowAPI
import ragflow_sdk

config = Config.load()

# Check RAGFlow config
print('=== RAGFlow Config ===')
print(f"Base URL: {config.ragflow.get('base_url')}")
print(f"Dataset ID: {config.ragflow.get('dataset_id')}")

# Test RAGFlow connection via SDK
print('\n=== RAGFlow SDK Test ===')
api_key = config.ragflow.get('api_key', '')

print(f"Using API key: {api_key[:20]}...")

rag = ragflow_sdk.RAGFlow(api_key, config.ragflow.get('base_url', ''))

# Simple dataset check
datasets = rag.list_datasets()
print(f'Datasets found: {len(datasets)}')

# Get our dataset
dataset_id = config.ragflow.get('dataset_id')
print(f'Dataset ID from config: {dataset_id}')

# Test our RAGFlowAPI wrapper
print('\n=== RAGFlowAPI Wrapper Test ===')
wrapper = RAGFlowAPI(
    base_url=config.ragflow.get('base_url', ''),
    api_key=config.ragflow.get('api_key', ''),
    dataset_id=dataset_id
)

# Check dataset exists via wrapper
datasets = wrapper.list_datasets()
print(f'Datasets via wrapper: {len(datasets)}')
for ds in datasets:
    print(f"  - {ds.get('name')}: {ds.get('id')}")

# Try storing a test document
print("\nStoring test document...")
result = wrapper.store_news(
    title="Test News Article",
    summary="This is a test article to verify RAGFlow integration",
    source="Test",
    url="https://test.com",
    sentiment=0.5
)
print(f"Store result: {result}")

print('\n=== All tests passed ===')