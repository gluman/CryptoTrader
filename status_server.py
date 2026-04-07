"""Simple HTTP status server for CryptoTrader"""
import sys
sys.path.insert(0, '.')

from src.core.config import Config
from src.core.database import DatabaseManager
from src.agents.telegram_notifier import TelegramNotifier
from flask import Flask, render_template_string, jsonify
from datetime import datetime
import logging

app = Flask(__name__)
config = Config.load()
logger = logging.getLogger('status_server')
db = DatabaseManager(config.postgresql, logger)
telegram = TelegramNotifier(
    bot_token=config.telegram.get('bot_token', ''),
    chat_id=config.telegram.get('chat_id', ''),
    logger=logger
)

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>CryptoTrader Status</title>
    <meta charset="utf-8">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { background: #2c3e50; color: white; padding: 20px; border-radius: 5px; }
        .section { background: white; padding: 20px; margin: 20px 0; border-radius: 5px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .metric { display: inline-block; margin: 10px 20px; }
        .metric-value { font-size: 24px; font-weight: bold; color: #3498db; }
        .metric-label { font-size: 12px; color: #7f8c8d; }
        .positive { color: #27ae60; }
        .negative { color: #e74c3c; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background: #ecf0f1; }
        .refresh { text-align: right; color: #7f8c8d; font-size: 12px; }
        @meta { refresh: 30; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 CryptoTrader Dashboard</h1>
            <p>Автоматический торговый бот | Обновление: {{ timestamp }}</p>
        </div>

        <div class="section">
            <h2>📊 Мгновенные метрики</h2>
            <div class="metric">
                <div class="metric-value">{{ stats.symbols_selected }}</div>
                <div class="metric-label">Символов выбрано</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ stats.ohlcv_count }}</div>
                <div class="metric-label">Записей OHLCV</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ stats.news_count }}</div>
                <div class="metric-label">Новостей</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ '%.3f'|format(stats.avg_sentiment) }}</div>
                <div class="metric-label">Сентимент</div>
            </div>
            <div class="metric">
                <div class="metric-value {{ 'positive' if stats.open_positions > 0 else '' }}">{{ stats.open_positions }}</div>
                <div class="metric-label">Открытых позиций</div>
            </div>
        </div>

        <div class="section">
            <h2>📈 Последние сигналы</h2>
            <table>
                <thead>
                    <tr>
                        <th>Время</th>
                        <th>Символ</th>
                        <th>Сигнал</th>
                        <th>Уверенность</th>
                        <th>RSI</th>
                        <th>Цена</th>
                    </tr>
                </thead>
                <tbody>
                {% for sig in signals %}
                    <tr>
                        <td>{{ sig.timestamp.strftime('%H:%M:%S') if sig.timestamp else 'N/A' }}</td>
                        <td>{{ sig.symbol }}</td>
                        <td class="{{ 'positive' if sig.signal_type == 'BUY' else 'negative' if sig.signal_type == 'SELL' else '' }}">
                            {{ sig.signal_type }}
                        </td>
                        <td>{{ '%.1f'|format(sig.confidence * 100) if sig.confidence else 'N/A' }}%</td>
                        <td>{{ '%.1f'|format(sig.rsi_14) if sig.rsi_14 else 'N/A' }}</td>
                        <td>{{ '%.2f'|format(sig.price) if sig.price else 'N/A' }}</td>
                    </tr>
                {% endfor %}
                {% if not signals %}
                    <tr><td colspan="6" style="text-align:center;">Нет данных</td></tr>
                {% endif %}
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>💼 Открытые позиции</h2>
            <table>
                <thead>
                    <tr>
                        <th>Символ</th>
                        <th>Сторона</th>
                        <th>Кол-во</th>
                        <th>Цена входа</th>
                        <th>PnL %</th>
                        <th>Stop Loss</th>
                        <th>Take Profit</th>
                    </tr>
                </thead>
                <tbody>
                {% for pos in positions %}
                    <tr>
                        <td>{{ pos.symbol }}</td>
                        <td class="{{ 'positive' if pos.side == 'LONG' else 'negative' }}">{{ pos.side }}</td>
                        <td>{{ '%.6f'|format(pos.quantity) }}</td>
                        <td>{{ '%.2f'|format(pos.entry_price) }}</td>
                        <td class="{{ 'positive' if pos.unrealized_pnl_percent and pos.unrealized_pnl_percent > 0 else 'negative' if pos.unrealized_pnl_percent and pos.unrealized_pnl_percent < 0 else '' }}">
                            {{ '%.2f'|format(pos.unrealized_pnl_percent) if pos.unrealized_pnl_percent else '0.00' }}%
                        </td>
                        <td>{{ '%.2f'|format(pos.stop_loss) if pos.stop_loss else '—' }}</td>
                        <td>{{ '%.2f'|format(pos.take_profit) if pos.take_profit else '—' }}</td>
                    </tr>
                {% endfor %}
                {% if not positions %}
                    <tr><td colspan="7" style="text-align:center;">Нет открытых позиций</td></tr>
                {% endif %}
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>📋 Логи агентов (последние 20)</h2>
            <table>
                <thead>
                    <tr>
                        <th>Время</th>
                        <th>Агент</th>
                        <th>Уровень</th>
                        <th>Сообщение</th>
                    </tr>
                </thead>
                <tbody>
                {% for log in logs %}
                    <tr>
                        <td>{{ log.timestamp.strftime('%H:%M:%S') if log.timestamp else 'N/A' }}</td>
                        <td>{{ log.agent_name }}</td>
                        <td style="color: 
                            {% if log.level == 'ERROR' %}red
                            {% elif log.level == 'WARNING' %}orange
                            {% else %}inherit{% endif %};">
                            {{ log.level }}
                        </td>
                        <td>{{ log.message[:100] }}{% if log.message|length > 100 %}...{% endif %}</td>
                    </tr>
                {% endfor %}
                {% if not logs %}
                    <tr><td colspan="4" style="text-align:center;">Нет логов</td></tr>
                {% endif %}
                </tbody>
            </table>
        </div>

        <p class="refresh">Страница обновляется автоматически каждые 30 секунд</p>
    </div>
</body>
</html>
'''

@app.route('/')
def dashboard():
    """Main dashboard page"""
    stats = get_system_stats()
    signals = get_recent_signals(limit=10)
    positions = get_open_positions()
    logs = get_recent_logs(limit=20)
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    
    return render_template_string(
        HTML_TEMPLATE,
        stats=stats,
        signals=signals,
        positions=positions,
        logs=logs,
        timestamp=timestamp
    )

@app.route('/api/status')
def api_status():
    """JSON API for external tools"""
    return jsonify({
        'status': 'ok' if db.test_connection() else 'error',
        'stats': get_system_stats(),
        'timestamp': datetime.utcnow().isoformat()
    })

def get_system_stats():
    """Get current system statistics"""
    with db.get_session() as session:
        from sqlalchemy import func
        from src.core.database import OHLCVRaw, NewsRaw, Signal, Position
        
        ohlcv_count = session.query(func.count(OHLCVRaw.id)).scalar() or 0
        news_count = session.query(func.count(NewsRaw.id)).scalar() or 0
        signals_today = session.query(func.count(Signal.id)).filter(
            Signal.created_at >= func.date(func.now())
        ).scalar() or 0
        open_positions = session.query(func.count(Position.id)).filter(
            Position.status == 'OPEN'
        ).scalar() or 0
        
        # Avg sentiment
        from src.agents.sentiment_agent import SentimentAgent
        sentiment_data = SentimentAgent(config, logger, db).get_aggregated_sentiment(hours=24)
        
        # Symbols selected today
        from src.core.database import SelectedSymbol
        symbols_selected = session.query(func.count(SelectedSymbol.id)).filter(
            SelectedSymbol.is_active == True
        ).scalar() or 0
        
        return {
            'symbols_selected': symbols_selected,
            'ohlcv_count': ohlcv_count,
            'news_count': news_count,
            'signals_today': signals_today,
            'open_positions': open_positions,
            'avg_sentiment': sentiment_data.get('avg_sentiment', 0.0),
        }

def get_recent_signals(limit=10):
    """Get recent trading signals"""
    with db.get_session() as session:
        from src.core.database import Signal
        signals = session.query(Signal).order_by(
            Signal.created_at.desc()
        ).limit(limit).all()
        return signals

def get_open_positions():
    """Get currently open positions"""
    with db.get_session() as session:
        from src.core.database import Position
        positions = session.query(Position).filter(
            Position.status == 'OPEN'
        ).order_by(Position.opened_at.desc()).all()
        return positions

def get_recent_logs(limit=20):
    """Get recent agent logs"""
    with db.get_session() as session:
        from src.core.database import AgentLog
        logs = session.query(AgentLog).order_by(
            AgentLog.timestamp.desc()
        ).limit(limit).all()
        return logs

if __name__ == '__main__':
    print("Starting CryptoTrader Status Server...")
    print("→ Dashboard: http://localhost:5000")
    print("→ API: http://localhost:5000/api/status")
    app.run(host='0.0.0.0', port=5000, debug=False)
