#!/bin/bash
API_KEY="ragflow-OJRiJt8hfSNSnIWgkRqhhz3kLmOO38qiRWqnpZ_8exs"
BASE="http://localhost:9380"

create_ds() {
    local name="$1"
    local desc="$2"
    local resp
    resp=$(curl -s -X POST \
        -H "Authorization: Bearer $API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"$name\",\"description\":\"$desc\"}" \
        "$BASE/api/v1/datasets")
    
    local id
    id=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',{}).get('id','ERROR'))" 2>/dev/null)
    echo "$name: $id"
}

create_ds "cryptotrader_news" "Crypto news articles with sentiment analysis for trading decisions"
create_ds "cryptotrader_expert_analysis" "Expert trading analysis and recommendations for crypto pairs"
create_ds "cryptotrader_strategies" "Trading strategies rules and technical analysis patterns"
create_ds "cryptotrader_journal" "Trading journal with entry/exit decisions and outcomes"
