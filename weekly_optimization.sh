#!/bin/bash
# weekly_optimization.sh — Bash wrapper для еженедельной оптимизации universe
#
# ЗАЧЕМ: Cron no_agent=True требует bash wrapper для логирования
#
# ЛОГИКА:
# 1. Запускает weekly_optimization.py
# 2. Логирует stdout/stderr в ~/.hermes/cron/output/weekly_optimization/
# 3. Возвращает exit code

set -e

LOG_DIR="$HOME/.hermes/cron/output/weekly_optimization"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/optimization_$TIMESTAMP.log"

echo "=== Weekly Optimization Start: $(date) ===" | tee "$LOG_FILE"

cd /home/andy/CryptoTrader_main

/home/andy/cryptotrader-venv/bin/python cryptotrader_strategies/weekly_optimization.py 2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=$?

echo "=== Weekly Optimization End: $(date), exit code: $EXIT_CODE ===" | tee -a "$LOG_FILE"

exit $EXIT_CODE
