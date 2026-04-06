#!/bin/bash
# Create CryptoTrader datasets in RAGFlow via local curl

BASE_URL="http://localhost:9380"
API_KEY="ragflow-OJRiJt8hfSNSnIWgkRqhhz3kLmOO38qiRWqnpZ_8exs"

HEADERS="Authorization: Bearer $API_KEY"
HEADERS2="Content-Type: application/json"

echo "Creating CryptoTrader datasets in RAGFlow..."
echo "Server: $BASE_URL"
echo ""

# Check existing datasets
echo "Checking existing datasets..."
EXISTING=$(curl -s -H "$HEADERS" "$BASE_URL/api/v1/datasets" 2>/dev/null)
echo "$EXISTING" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    datasets = data.get('data', [])
    for ds in datasets:
        print(f\"  Found: {ds.get('name')} (ID: {ds.get('id')})\")
except:
    print('  Could not parse response')
" 2>/dev/null

# Create datasets
DATASETS='[
  {"name": "cryptotrader_news", "description": "Crypto news articles with sentiment analysis for trading decisions"},
  {"name": "cryptotrader_expert_analysis", "description": "Expert trading analysis and recommendations for crypto pairs"},
  {"name": "cryptotrader_strategies", "description": "Trading strategies, rules, and technical analysis patterns"},
  {"name": "cryptotrader_journal", "description": "Trading journal with entry/exit decisions and outcomes"}
]'

echo "$DATASETS" | python3 -c "
import sys, json, subprocess

datasets = json.load(sys.stdin)
base_url = 'http://localhost:9380'
api_key = 'ragflow-OJRiJt8hfSNSnIWgkRqhhz3kLmOO38qiRWqnpZ_8exs'
headers = f'Authorization: Bearer {api_key}'

# Get existing
result = subprocess.run(['curl', '-s', '-H', headers, f'{base_url}/api/v1/datasets'], capture_output=True, text=True)
existing_names = set()
try:
    data = json.loads(result.stdout)
    for ds in data.get('data', []):
        existing_names.add(ds.get('name'))
except:
    pass

created = []
for ds in datasets:
    name = ds['name']
    if name in existing_names:
        print(f'  ✅ {name}: already exists')
        continue
    
    resp = subprocess.run([
        'curl', '-s', '-X', 'POST',
        '-H', headers,
        '-H', 'Content-Type: application/json',
        '-d', json.dumps(ds),
        f'{base_url}/api/v1/datasets'
    ], capture_output=True, text=True)
    
    try:
        data = json.loads(resp.stdout)
        ds_id = data.get('data', {}).get('id', 'unknown')
        print(f'  ✅ {name}: created (ID: {ds_id})')
        created.append({'name': name, 'id': ds_id})
    except:
        print(f'  ❌ {name}: failed - {resp.stdout[:200]}')

print()
print('Dataset IDs for config:')
for c in created:
    key = c['name'].replace('cryptotrader_', '')
    print(f'    {key}: \"{c[\"id\"]}\"')
"
