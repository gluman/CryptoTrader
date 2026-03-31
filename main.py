import sys
import os
import argparse
import time
import logging
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.config import Config
from src.core.database import DatabaseManager
from src.core.logger import setup_logger
from src.agents import (
    DataCollectorAgent, SentimentAgent, TradingDecisionAgent,
    ExecutionAgent, TelegramNotifier
)


def main():
    parser = argparse.ArgumentParser(description='CryptoTrader - Multi-Agent Trading System')
    parser.add_argument('--task', choices=['collect', 'sentiment', 'decide', 'execute', 'all', 'api'],
                        default='all', help='Task to run')
    parser.add_argument('--symbols', nargs='+', help='Symbols to process')
    parser.add_argument('--exchange', default='binance', help='Exchange to use')
    parser.add_argument('--config', default=None, help='Path to config file')
    
    args = parser.parse_args()
    
    # Load config
    config = Config.load(args.config)
    
    # Setup logger
    logger = setup_logger(
        'cryptotrader',
        level=config.logging.get('level', 'INFO'),
        log_file=config.logging.get('file'),
    )
    
    logger.info(f"CryptoTrader starting with task: {args.task}")
    
    # Initialize database
    db = DatabaseManager(config.postgresql, logger)
    db.create_tables()
    
    # Initialize agents
    sentiment_agent = SentimentAgent(config, logger, db)
    data_collector = DataCollectorAgent(config, logger, db)
    trading_agent = TradingDecisionAgent(config, logger, db, sentiment_agent)
    executor = ExecutionAgent(config, logger, db)
    
    # Telegram notifications
    telegram = None
    if config.telegram.get('enabled') and config.telegram.get('chat_id'):
        telegram = TelegramNotifier(
            config.telegram['bot_token'],
            config.telegram['chat_id'],
            logger
        )
        telegram.verify_connection()
    
    # Run task
    if args.task == 'api':
        # Start FastAPI server
        logger.info("Starting FastAPI server on port 8000...")
        import uvicorn
        from src.api.server import app
        uvicorn.run(app, host="0.0.0.0", port=8000)
    
    elif args.task == 'collect':
        result = data_collector.run_once()
        logger.info(f"Collection result: {result}")
    
    elif args.task == 'sentiment':
        result = sentiment_agent.run_once()
        logger.info(f"Sentiment result: {result}")
    
    elif args.task == 'decide':
        if args.symbols:
            for sym in args.symbols:
                result = trading_agent.run_once_for_symbol(sym, args.exchange)
                logger.info(f"Decision for {sym}: {result}")
                if telegram and result.get('signal') != 'HOLD':
                    telegram.notify_signal(sym, result['signal'], result['confidence'], result.get('reasoning', ''))
        else:
            result = trading_agent.run_once()
            logger.info(f"Full cycle result: {result}")
    
    elif args.task == 'execute':
        result = executor.run_once()
        logger.info(f"Execution result: {result}")
    
    elif args.task == 'all':
        # Full pipeline
        logger.info("Running full pipeline...")
        
        # 1. Collect data
        logger.info("Step 1: Collecting data...")
        collect_result = data_collector.run_once()
        logger.info(f"Collected: {collect_result}")
        
        # 2. Analyze sentiment
        logger.info("Step 2: Analyzing sentiment...")
        sentiment_result = sentiment_agent.run_once()
        logger.info(f"Sentiment: {sentiment_result}")
        
        # 3. Generate trading decisions
        logger.info("Step 3: Generating decisions...")
        decision_result = trading_agent.run_once()
        logger.info(f"Decisions: {decision_result}")
        
        # 4. Execute pending signals
        logger.info("Step 4: Executing signals...")
        exec_result = executor.run_once()
        logger.info(f"Executed: {exec_result}")
        
        # 5. Send summary
        if telegram:
            summary = {
                'total_signals': len(decision_result.get('decisions', [])),
                'buys': decision_result.get('summary', {}).get('buys', 0),
                'sells': decision_result.get('summary', {}).get('sells', 0),
                'holds': decision_result.get('summary', {}).get('holds', 0),
                'trades_executed': exec_result.get('executed', 0),
            }
            telegram.notify_daily_summary(summary)
        
        logger.info("Full pipeline complete!")
    
    logger.info("CryptoTrader finished")


if __name__ == '__main__':
    main()
