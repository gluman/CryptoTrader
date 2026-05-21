"""Test: DeepSeek with real indicators"""
import sys; sys.path.insert(0, '.')
import json, time
from src.core.config import Config; Config._instance = None; config = Config.load()
from src.core.logger import setup_logger
logger = setup_logger('test_ds', level='INFO')
from src.core.database import DatabaseManager
db = DatabaseManager(config.postgresql, logger)
from src.agents.sentiment_agent import SentimentAgent
from src.agents.trading_agent import TradingDecisionAgent

sentiment = SentimentAgent(config, logger, db)
trading = TradingDecisionAgent(config, logger, db, sentiment)

df = trading.get_ohlcv_data('ENJUSDT', 'binance', '1h')
print(f'DataFrame shape: {df.shape}')
if df.empty:
    print('NO DATA — bailing out')
    sys.exit(1)

print(f'Date range: {df.index[0]} to {df.index[-1]}')
latest = df.iloc[-1]
print(f'Latest: open={latest.open:.4f} high={latest.high:.4f} low={latest.low:.4f} close={latest.close:.4f} vol={latest.volume:.2f}')

indicators = trading.calculate_indicators(df)
ind_s = {k: round(v,6) if isinstance(v, float) else v for k,v in indicators.items()}
print(f'Indicators: {json.dumps(ind_s, indent=2)}')

senti = sentiment.get_aggregated_sentiment(hours=24)
recent = trading.get_recent_signals('ENJUSDT')
positions = trading.get_open_positions_for_symbol('ENJUSDT')
prompt = trading.build_prompt('ENJUSDT', indicators, senti, recent, positions, '')
print(f'Prompt length: {len(prompt)} chars')

t0 = time.time()
decision = trading.call_llm(prompt)
dt = time.time()-t0

print(f'\nDeepSeek ({dt:.1f}s): {decision["signal"]} (conf={decision["confidence"]:.0%})')
print(f'  Reasoning: {decision.get("reasoning","")[:200]}')
print(f'  SL: {decision.get("stop_loss")} TP: {decision.get("take_profit")}')
