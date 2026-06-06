#!/usr/bin/env python3
"""
Clone5 v6 live scanner — cron-friendly version.
Каждые 60 мин сканирует пары, генерирует сигналы, создаёт strategy_signals.

Использование:
    python clone5_v6_cron.py
"""
import os
import sys
import json
from datetime import datetime, timezone

sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import psycopg2
from cryptotrader_strategies.clone5_live_runner import scan_once

if __name__ == "__main__":
    print(f"=== Clone5 v6 cron scan: {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    try:
        n = scan_once()
        result = {"status": "ok", "signals": n, "ts": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        result = {"status": "error", "error": str(e), "ts": datetime.now(timezone.utc).isoformat()}
    print(json.dumps(result, indent=2), flush=True)
