#!/bin/bash
# execute_cron.sh — обёртка для cron (no_agent=True).
# Запускает execute_cron.py каждые 3 мин, output сохраняется планировщиком.
set -e
cd /home/andy/CryptoTrader_main
exec /home/andy/cryptotrader-venv/bin/python cryptotrader_strategies/execute_cron.py 2>&1
