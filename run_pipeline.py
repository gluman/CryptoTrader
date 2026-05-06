"""Run full pipeline
Usage:
  python run_pipeline.py rule --market spot   — Spot trading only (default)
  python run_pipeline.py rule --market linear — Linear futures only
  python run_pipeline.py llm --market spot     — LLM evaluated, spot
  python run_pipeline.py test                  — Test mode both
"""
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else 'llm'  # 'llm', 'rule', 'test'
MARKET = 'spot'
for arg in sys.argv:
    if arg.startswith('--market='):
        MARKET = arg.split('=')[1]  # 'spot' or 'linear'
    elif arg == '--spot':
        MARKET = 'spot'
    elif arg == '--linear':
        MARKET = 'linear'

# Reset config singleton
from src.core.config import Config
Config._instance = None

from src.core.database import DatabaseManager
from src.core.logger import setup_logger
from src.agents import DataCollectorAgent, SentimentAgent, TradingDecisionAgent, ExecutionAgent
from src.agents.telegram_notifier import TelegramNotifier

config = Config.load()
logger = setup_logger('run', level='INFO', log_file=config.logging.get('file'))

print("=" * 60)
print("CRYPTO TRADER - RUNNING FULL PIPELINE")
print("=" * 60)
print(f"PostgreSQL: {config.postgresql.get('host')}:{config.postgresql.get('port')}")
print(f"Market type: {MARKET}")
print(f"Mode: {MODE}")
print("=" * 60)

# Init Telegram notifier
telegram = TelegramNotifier(
    bot_token=config.telegram.get('bot_token', ''),
    chat_id=config.telegram.get('chat_id', ''),
    logger=logger
)
# Verify Telegram connection
if telegram.verify_connection():
    logger.info("Telegram bot connected successfully")
else:
    logger.warning("Telegram bot not configured or connection failed")

# Init
db = DatabaseManager(config.postgresql, logger)
db.create_tables()

sentiment = SentimentAgent(config, logger, db)
data_collector = DataCollectorAgent(config, logger, db)
trading = TradingDecisionAgent(config, logger, db, sentiment)
executor = ExecutionAgent(config, logger, db)

try:
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
    print(f"\n[3/4] Generating trading decisions ({MODE}-based)...")
    if MODE == 'rule':
        decision_result = trading.run_once_rule_based()
    elif MODE == 'llm':
        decision_result = trading.run_once_llm_evaluated()
    elif MODE == 'test':
        print("  [TEST MODE] Running both rule-based and LLM-evaluated...")
        rule_result = trading.run_once_rule_based()
        llm_result = trading.run_once_llm_evaluated()
        decision_result = {'rule': rule_result, 'llm': llm_result, 'mode': 'test'}
        print(f"  Rule-based: BUY={rule_result['summary']['buys']}, SELL={rule_result['summary']['sells']}, HOLD={rule_result['summary']['holds']}")
        print(f"  LLM-eval:   BUY={llm_result['summary']['buys']}, SELL={llm_result['summary']['sells']}, HOLD={llm_result['summary']['holds']}")
        decision_result = rule_result  # Use rule-based for execution
    else:
        decision_result = trading.run_once_llm_evaluated()
    summary = decision_result.get('summary', {})
    print(f"  BUY: {summary.get('buys', 0)}")
    print(f"  SELL: {summary.get('sells', 0)}")
    print(f"  HOLD: {summary.get('holds', 0)}")
    
    # Step 4: Execute
    print(f"\n[4/4] Executing trades ({MARKET})...")
    exec_result = executor.run_once(market_type=MARKET)
    print(f"  Executed: {exec_result.get('executed', 0)}")
    print(f"  SL/TP triggered: {exec_result.get('sl_tp_triggered', 0)}")
    
    positions = executor.get_all_open_positions()
    if positions:
        print(f"\n  Open positions ({len(positions)}):")
        for p in positions:
            if isinstance(p, dict):
                print(f"    {p['symbol']} ({p.get('market_type','?')}): entry=${p.get('entry_price',0):.2f}, qty={p.get('quantity',0):.6f}")
                print(f"      SL={p.get('stop_loss','N/A')}, TP={p.get('take_profit','N/A')}")
            else:
                print(f"    {p.symbol}: entry={float(p.entry_price):.2f}, qty={float(p.quantity):.6f}")
                print(f"      SL={float(p.stop_loss) if p.stop_loss else 'N/A'}, TP={float(p.take_profit) if p.take_profit else 'N/A'}")
    
    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    
    # Send success notification
    if telegram.chat_id:
        telegram.send_message(
            f"✅ <b>Pipeline completed successfully</b>\n"
            f"Symbols: {collect_result.get('symbols_selected', 0)}\n"
            f"OHLCV: {collect_result.get('ohlcv_records', 0)}\n"
            f"News: {collect_result.get('news_records', 0)}\n"
            f"Sentiment: {agg.get('avg_sentiment', 0):.3f}\n"
            f"Signals: BUY {summary.get('buys', 0)}, SELL {summary.get('sells', 0)}\n"
            f"Trades: {exec_result.get('executed', 0)}\n"
            f"Open positions: {len(positions)}"
        )

except Exception as e:
    logger.exception("Pipeline failed")
    # Send error notification
    telegram.notify_error('Pipeline', str(e))
    raise
