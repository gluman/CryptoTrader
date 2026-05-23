#!/bin/bash
# Start CryptoTrader Scheduler with proper environment
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load environment variables from project .env
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

# Verify DeepSeek key is available
if [ -z "$DEEPSEEK_API_KEY" ]; then
    echo "ERROR: DEEPSEEK_API_KEY not set in $PROJECT_ROOT/.env"
    exit 1
fi

cd "$PROJECT_ROOT"

# Set PYTHONPATH so scheduler.py can import src.*
export PYTHONPATH="$PROJECT_ROOT:$SCRIPT_DIR"

# Use the venv Python if available, otherwise system python
if [ -f "$PROJECT_ROOT/cryptotrader-venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/cryptotrader-venv/bin/python"
elif [ -f "/home/andy/cryptotrader-venv/bin/python" ]; then
    PYTHON="/home/andy/cryptotrader-venv/bin/python"
elif [ -f "$PROJECT_ROOT/../cryptotrader-venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/../cryptotrader-venv/bin/python"
else
    PYTHON="python3"
fi

exec $PYTHON "$SCRIPT_DIR/scheduler.py" "$@"