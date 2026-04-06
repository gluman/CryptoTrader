#!/bin/bash
API='ragflow-OJRiJt8hfSNSnIWgkRqhhz3kLmOO38qiRWqnpZ_8exs'
URL='http://localhost:9380'

create() {
    local name=$1
    local desc=$2
    local resp
    resp=$(curl -s -X POST \
        -H "Authorization: Bearer $API" \
        -H 'Content-Type: application/json' \
        -d '{"name":"'"$name"'","description":"'"$desc"'"}' \
        "$URL/api/v1/datasets")
    local id
    id=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',{}).get('id','ERROR'))" 2>/dev/null)
    echo "  $name -> $id"
}

echo "Creating CryptoTrader datasets in RAGFlow..."
create cryptotrader_news 'Crypto news with sentiment analysis for trading decisions'
create cryptotrader_expert_analysis 'Expert trading analysis and recommendations'
create cryptotrader_strategies 'Trading strategies rules and technical patterns'
create cryptotrader_journal 'Trading journal with entry/exit decisions'
echo "Done!"
