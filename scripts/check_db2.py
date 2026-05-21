#!/usr/bin/env python3
import subprocess, os

env_file = '/home/andy/CryptoTrader/.env'
with open(env_file) as f:
    env = {}
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            env[k] = v
pw = env.get('POSTGRES_PASSWORD', '')

# strategy_signals without market_type
cmd1 = f'PGPASSWORD={pw} psql -h 127.0.0.1 -p 5433 -U cryptotrader -d cryptotrader -c "SELECT id, symbol, action, confidence, status, created_at FROM strategy_signals WHERE action IN (\'BUY\',\'SELL\') AND status=\'pending\' ORDER BY created_at DESC LIMIT 10;"'
print("=== STRATEGY_SIGNALS (pending) ===")
r = subprocess.run(cmd1, shell=True, capture_output=True, text=True)
print(r.stdout or r.stderr)

# signals table pending
cmd2 = f'PGPASSWORD={pw} psql -h 127.0.0.1 -p 5433 -U cryptotrader -d cryptotrader -c "SELECT id, symbol, signal_type, confidence, status, market_type, created_at FROM signals WHERE signal_type IN (\'BUY\',\'SELL\') AND status=\'PENDING\' ORDER BY created_at DESC LIMIT 10;"'
print("=== SIGNALS (pending) ===")
r = subprocess.run(cmd2, shell=True, capture_output=True, text=True)
print(r.stdout or r.stderr)
