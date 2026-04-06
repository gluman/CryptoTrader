"""Run full pipeline"""
import sys
sys.path.insert(0, '.')

# Reset config singleton
from src.core.config import Config
Config._instance = None

from src.core.database import DatabaseManager
from src.core.logger import setup_logger
from src.agents import DataCollectorAgent, SentimentAgent, TradingDecisionAgent, ExecutionAgent

config = Config.load()
logger = setup_logger('run', level='INFO', log_file=config.logging.get('file'))

print("=" * 60)
print("CRYPTO TRADER - RUNNING FULL PIPELINE")
print("=" * 60)
print(f"PostgreSQL: {config.postgresql.get('host')}:{config.postgresql.get('port')}")
print(f"Binance testnet: {config.binance.get('testnet')}")
print(f"Model: {config.openrouter.get('model')}")
print("=" * 60)

# Init
db = DatabaseManager(config.postgresql, logger)
db.create_tables()

sentiment = SentimentAgent(config, logger, db)
data_collector = DataCollectorAgent(config, logger, db)
trading = TradingDecisionAgent(config, logger, db, sentiment)
executor = ExecutionAgent(config, logger, db)

# Step 1: Collect data
print("\n[1/4] Collecting market data...")
collect_result = data_collector.run_once()
print(f"  Symbols: {collect_result.get('symbols_selected', 0)}")
print(f"  OHLCV records: {collect_result.get('ohlcv_records', 0)}")
print(f"  News: {collect_result.get('news_records', 0)}")

# Step 2: Sentiment
print("\n[2/4] Analyzing sentiment...")
sentiment_result = sentiment.run_once()
print(f"  Analyzed: {sentiment_result.get('analyzed', 0)}")
agg = sentiment_result.get('aggregated', {})
print(f"  Avg sentiment: {agg.get('avg_sentiment', 0):.3f}")
print(f"  Bullish ratio: {agg.get('bullish_ratio', 0):.0%}")

# Step 3: Trading decisions
print("\n[3/4] Generating trading decisions...")
decision_result = trading.run_once()
summary = decision_result.get('summary', {})
print(f"  BUY: {summary.get('buys', 0)}")
print(f"  SELL: {summary.get('sells', 0)}")
print(f"  HOLD: {summary.get('holds', 0)}")

# Step 4: Execute
print("\n[4/4] Executing trades...")
exec_result = executor.run_once()
print(f"  Executed: {exec_result.get('executed', 0)}")
print(f"  SL/TP triggered: {exec_result.get('sl_tp_triggered', 0)}")

positions = executor.get_all_open_positions()
if positions:
    print(f"\n  Open positions ({len(positions)}):")
    for p in positions:
        print(f"    {p.symbol}: entry={float(p.entry_price):.2f}, qty={float(p.quantity):.6f}")
        print(f"      SL={float(p.stop_loss) if p.stop_loss else 'N/A'}, TP={float(p.take_profit) if p.take_profit else 'N/A'}")

print("\n" + "=" * 60)
print("PIPELINE COMPLETE")
print("=" * 60)
