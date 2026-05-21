#!/usr/bin/env python3
import subprocess, os

env_file = '/home/andy/CryptoTrader/.env'
# Read POSTGRES_PASSWORD from .env
with open(env_file) as f:
    env = {}
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            env[k] = v

pw = env.get('POSTGRES_PASSWORD', '')

# StrategySignal table
cmd1 = f'PGPASSWORD={pw} psql -h 127.0.0.1 -p 5433 -U cryptotrader -d cryptotrader -c "SELECT id, symbol, action, confidence, status, market_type, created_at FROM strategy_signals WHERE action IN (\'BUY\',\'SELL\') AND status=\'pending\' ORDER BY created_at DESC LIMIT 10;"'
print("=== STRATEGY_SIGNALS (pending) ===")
r = subprocess.run(cmd1, shell=True, capture_output=True, text=True)
print(r.stdout or r.stderr)

# Positions open
cmd2 = f'PGPASSWORD={pw} psql -h 127.0.0.1 -p 5433 -U cryptotrader -d cryptotrader -c "SELECT id, symbol, side, entry_price, quantity, stop_loss, take_profit, unrealized_pnl, status, market_type FROM positions WHERE status=\'OPEN\' ORDER BY created_at DESC LIMIT 10;"'
print("\n=== OPEN POSITIONS ===")
r = subprocess.run(cmd2, shell=True, capture_output=True, text=True)
print(r.stdout or r.stderr)

# Recent trades
cmd3 = f'PGPASSWORD={pw} psql -h 127.0.0.1 -p 5433 -U cryptotrader -d cryptotrader -c "SELECT id, symbol, side, quantity, price, pnl_absolute, pnl_percent, created_at FROM trades ORDER BY created_at DESC LIMIT 5;"'
print("\n=== RECENT TRADES ===")
r = subprocess.run(cmd3, shell=True, capture_output=True, text=True)
print(r.stdout or r.stderr)
