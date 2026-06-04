#!/bin/bash
echo "=========================================="
echo "📊 CryptoTrader Status Check"
echo "=========================================="

cd ~/CryptoTrader

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

check_process() {
    local pattern=$1
    local name=$2
    local pid_file=$3
    
    if [ -f "$pid_file" ]; then
        PID=$(cat "$pid_file")
        if ps -p $PID > /dev/null 2>&1; then
            echo -e "  ${GREEN}✓${NC} $name (PID $PID)"
            return 0
        else
            echo -e "  ${RED}✗${NC} $name (PID file exists but process dead)"
            return 1
        fi
    fi
    
    # Fallback: grep process
    PID=$(pgrep -f "$pattern" | head -1)
    if [ -n "$PID" ]; then
        echo -e "  ${GREEN}✓${NC} $name (PID $PID)"
        echo $PID > "$pid_file" 2>/dev/null || true
        return 0
    else
        echo -e "  ${RED}✗${NC} $name NOT RUNNING"
        return 1
    fi
}

check_port() {
    local port=$1
    local service=$2
    
    if command -v ss &> /dev/null; then
        if ss -tlnp 2>/dev/null | grep -q ":$port "; then
            echo -e "  ${GREEN}✓${NC} $service listening on port $port"
            return 0
        fi
    elif command -v netstat &> /dev/null; then
        if netstat -tlnp 2>/dev/null | grep -q ":$port "; then
            echo -e "  ${GREEN}✓${NC} $service listening on port $port"
            return 0
        fi
    fi
    
    echo -e "  ${RED}✗${NC} $service NOT listening on port $port"
    return 1
}

echo ""
echo "Processes:"
check_process "run_pipeline.py" "Pipeline" "logs/pipeline.pid"
check_process "status_server.py" "Status Server" "logs/status_server.pid"

echo ""
echo "Network:"
check_port 5000 "Status Server"

echo ""
echo "Database:"
if command -v psql &> /dev/null; then
    if psql -h 192.168.0.149 -U cryptotrader -d cryptotrader -c "SELECT 1" &>/dev/null; then
        echo -e "  ${GREEN}✓${NC} PostgreSQL connection OK"
    else
        echo -e "  ${RED}✗${NC} PostgreSQL connection FAILED"
    fi
else
    echo "  ⚠️  psql not installed, skipping DB check"
fi

echo ""
echo "Recent logs (last 5 lines):"
echo "  --- Pipeline ---"
if [ -f logs/cryptotrader.log ]; then
    tail -5 logs/cryptotrader.log | sed 's/^/    /' || true
else
    echo "    No log file"
fi

echo "  --- Status Server ---"
if [ -f logs/status_server.log ]; then
    tail -5 logs/status_server.log | sed 's/^/    /' || true
else
    echo "    No log file"
fi

echo ""
echo "=========================================="
echo "Quick commands:"
echo "  tail -f logs/cryptotrader.log    # watch pipeline"
echo "  tail -f logs/status_server.log  # watch status server"
echo "  ./deploy.sh                     # redeploy"
echo "=========================================="
