#!/usr/bin/env python3
"""Multi-strategy cron wrapper — used by Hermes cron e020bee290d2."""
import os, sys, json
from datetime import datetime, timezone
sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')
from cryptotrader_strategies.clone5_multi_runner import main as run_multi

if __name__ == "__main__":
    print(f"=== Multi-strategy scan: {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    try:
        n = run_multi()
        result = {"status": "ok", "signals": n, "ts": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        result = {"status": "error", "error": str(e), "ts": datetime.now(timezone.utc).isoformat()}
    print(json.dumps(result, indent=2), flush=True)
