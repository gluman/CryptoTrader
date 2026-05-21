"""
Create RAGFlow datasets for CryptoTrader
"""
import requests
import json
import sys

BASE_URL = "http://192.168.0.186:9380"
API_KEY = "ragflow-OJRiJt8hfSNSnIWgkRqhhz3kLmOO38qiRWqnpZ_8exs"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

datasets = [
    {
        "name": "cryptotrader_news",
        "description": "Crypto news articles with sentiment analysis for trading decisions"
    },
    {
        "name": "cryptotrader_expert_analysis",
        "description": "Expert trading analysis and recommendations for crypto pairs"
    },
    {
        "name": "cryptotrader_strategies",
        "description": "Trading strategies, rules, and technical analysis patterns"
    },
    {
        "name": "cryptotrader_journal",
        "description": "Trading journal with entry/exit decisions and outcomes"
    }
]

print("Creating RAGFlow datasets for CryptoTrader...")
print(f"Server: {BASE_URL}")
print()

created = []

for ds in datasets:
    print(f"Creating: {ds['name']}...")
    
    # Check if exists
    resp = requests.get(f"{BASE_URL}/api/v1/datasets", headers=headers, timeout=10)
    if resp.status_code == 200:
        existing = resp.json().get("data", [])
        found = None
        for e in existing:
            if e.get("name") == ds["name"]:
                found = e
                break
        
        if found:
            print(f"  ✅ Already exists: ID={found['id']}")
            created.append({"name": ds["name"], "id": found["id"]})
            continue
    
    # Create new
    resp = requests.post(
        f"{BASE_URL}/api/v1/datasets",
        headers=headers,
        json=ds,
        timeout=10
    )
    
    if resp.status_code in [200, 201]:
        data = resp.json()
        ds_id = data.get("data", {}).get("id", "unknown")
        print(f"  ✅ Created: ID={ds_id}")
        created.append({"name": ds["name"], "id": ds_id})
    else:
        print(f"  ❌ Failed: {resp.status_code} - {resp.text[:200]}")

print()
print("=" * 60)
print("Datasets created:")
for c in created:
    print(f"  {c['name']}: {c['id']}")

print()
print("Add these to config/settings.yaml:")
for c in created:
    key = c['name'].replace('cryptotrader_', '')
    print(f"    {key}: \"{c['id']}\"")
