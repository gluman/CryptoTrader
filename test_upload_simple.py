import requests

api_key = "ragflow-OJRiJt8hfSNSnIWgkRqhhz3kLmOO38qiRWqnpZ_8exs"
base_url = "http://192.168.0.186:9380"
dataset_id = "dcaa90d231d711f199937e8f52fe67f3"

session = requests.Session()
session.headers.update({'Authorization': f'Bearer {api_key}'})

# Upload document
url = f"{base_url}/api/v1/datasets/{dataset_id}/documents"
files = {'file': ('test.txt', b'Test content for RAG', 'text/plain')}

resp = session.post(url, files=files, timeout=60)
print(f"Status: {resp.status_code}")
print(f"Response: {resp.text}")