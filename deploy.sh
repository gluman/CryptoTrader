#!/bin/bash
set -e

echo "=========================================="
echo "🚀 CryptoTrader Complete Deploy"
echo "=========================================="

cd ~/CryptoTrader

# 1. Git pull (если есть .git)
if [ -d .git ]; then
    echo "📥 Pulling latest code..."
    git pull || echo "⚠️  Git pull failed, continuing with existing code"
fi

# 2. Ensure virtual environment
if [ ! -d ~/cryptotrader-venv ]; then
    echo "🐍 Creating virtual environment..."
    uv venv cryptotrader-venv
fi

source ~/cryptotrader-venv/bin/activate

# 3. Install dependencies
echo "📦 Installing dependencies..."
pip install -r requirements.txt --quiet

# 4. Ensure logs directory
mkdir -p logs

# 5. Database migration
echo "🗄️  Running database migration..."
python migrate_db.py

# 6. Stop old services
echo "🛑 Stopping old services..."
pkill -f run_pipeline.py 2>/dev/null || true
pkill -f status_server.py 2>/dev/null || true
sleep 2

# 7. Start pipeline
echo "▶️  Starting pipeline..."
nohup python run_pipeline.py > logs/cryptotrader.log 2>&1 &
PIPELINE_PID=$!
echo $PIPELINE_PID > logs/pipeline.pid
echo "   PID: $PIPELINE_PID"

# 8. Start status server
echo "🌐 Starting status server..."
nohup python status_server.py > logs/status_server.log 2>&1 &
STATUS_PID=$!
echo $STATUS_PID > logs/status_server.pid
echo "   PID: $STATUS_PID"

# 9. Wait and check
sleep 5

echo ""
echo "=========================================="
echo "✅ Deploy Complete"
echo "=========================================="
echo ""
echo "📊 Services:"
if ps -p $PIPELINE_PID > /dev/null 2>&1; then
    echo "  ✅ Pipeline (PID $PIPELINE_PID)"
else
    echo "  ❌ Pipeline failed to start"
fi

if ps -p $STATUS_PID > /dev/null 2>&1; then
    echo "  ✅ Status Server (PID $STATUS_PID) → http://0.0.0.0:5000"
else
    echo "  ❌ Status Server failed to start"
fi

echo ""
echo "🌐 Access URLs:"
echo "  Dashboard: http://192.168.0.43:5000"
echo "  API:       http://192.168.0.43:5000/api/status"
echo ""
echo "📝 Logs:"
echo "  Pipeline: tail -f logs/cryptotrader.log"
echo "  Status:   tail -f logs/status_server.log"
echo ""
echo "🛑 Stop services:"
echo "  pkill -f run_pipeline.py"
echo "  pkill -f status_server.py"
echo ""
echo "🔧 Restart:"
echo "  ./deploy.sh"
echo ""
