import os
os.environ.setdefault('CONFIG_PATH', 'config/settings.yaml')

from src.core.config import Config
import ragflow_sdk

config = Config.load()

# Get API key - SDK expects the key WITHOUT 'ragflow-' prefix
api_key = config.ragflow.get('api_key', '')
if api_key.startswith('ragflow-'):
    api_key = api_key[8:]  # Remove 'ragflow-' prefix

base_url = config.ragflow.get('base_url', '')
dataset_id = config.ragflow.get('dataset_id')

print(f"API key: {api_key[:20]}...")
print(f"Base URL: {base_url}")
print(f"Dataset ID: {dataset_id}")

# Use SDK to get dataset
rag = ragflow_sdk.RAGFlow(api_key, base_url)

# Get the dataset object
datasets = rag.list_datasets()
ds = next((d for d in datasets if d['id'] == dataset_id), None)
print(f"Dataset: {ds}")

if ds:
    # Upload document using SDK
    from io import BytesIO
    
    content = "Test article about Bitcoin trading. This is a test document."
    filename = "test_bitcoin.txt"
    
    # Create file-like object
    file_obj = BytesIO(content.encode('utf-8'))
    file_obj.name = filename
    
    try:
        # Try with files parameter
        result = ds.upload_documents([
            {'file': (filename, BytesIO(content.encode('utf-8')), 'text/plain')}
        ])
        print(f"Upload result (files): {result}")
    except Exception as e:
        print(f"Error: {e}")
        
    # Try simple text upload
    try:
        result = ds.upload_documents([
            {'content': content, 'name': filename}
        ])
        print(f"Upload result (content): {result}")
    except Exception as e:
        print(f"Error: {e}")