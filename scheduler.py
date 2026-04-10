import sys
import os
import time
import logging
import signal
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.config import Config
from src.core.database import DatabaseManager
from src.core.logger import setup_logger
from src.agents import (
    DataCollectorAgent, SentimentAgent, TradingDecisionAgent,
    ExecutionAgent, TelegramNotifier
)


class CryptoTraderScheduler:
    """Unified scheduler for all CryptoTrader agents"""
    
    def __init__(self):
        self.running = False
        self.config = None
        self.logger = None
        self.db = None
        self.agents = {}
        
        # Schedule intervals (seconds)
        self.intervals = {
            'collect': 600,      # 10 minutes
            'sentiment': 1800,   # 30 minutes
            'decide': 900,      # 15 minutes
            'execute': 300,      # 5 minutes (check SL/TP)
        }
        
        self.last_run = {
            'collect': 0,
            'sentiment': 0,
            'decide': 0,
            'execute': 0,
        }
    
    def setup(self, config_path=None):
        """Initialize all components"""
        self.config = Config.load(config_path)
        
        self.logger = setup_logger(
            'cryptotrader.scheduler',
            level=self.config.logging.get('level', 'INFO'),
            log_file=self.config.logging.get('file'),
        )
        
        self.db = DatabaseManager(self.config.postgresql, self.logger)
        self.db.create_tables()
        
        # Initialize agents
        sentiment = SentimentAgent(self.config, self.logger, self.db)
        data_collector = DataCollectorAgent(self.config, self.logger, self.db)
        trading = TradingDecisionAgent(self.config, self.logger, self.db, sentiment)
        executor = ExecutionAgent(self.config, self.logger, self.db)
        
        self.agents = {
            'collect': data_collector,
            'sentiment': sentiment,
            'decide': trading,
            'execute': executor,
        }
        
        # Telegram
        self.telegram = None
        if self.config.telegram.get('enabled') and self.config.telegram.get('chat_id'):
            self.telegram = TelegramNotifier(
                self.config.telegram['bot_token'],
                self.config.telegram['chat_id'],
                self.logger
            )
        
        self.logger.info("CryptoTrader Scheduler initialized")
    
    def should_run(self, task: str) -> bool:
        """Check if task should run based on interval"""
        elapsed = time.time() - self.last_run[task]
        return elapsed >= self.intervals[task]
    
    def run_task(self, task: str):
        """Execute a single task"""
        try:
            self.logger.info(f"Running task: {task}")
            agent = self.agents[task]
            result = agent.run_once()
            self.last_run[task] = time.time()
            
            self.logger.info(f"Task {task} completed: {result}")
            
            # Notify on Telegram if needed
            if self.telegram and task == 'decide':
                summary = result.get('summary', {})
                if summary.get('buys', 0) > 0 or summary.get('sells', 0) > 0:
                    self.telegram.notify_daily_summary({
                        'total_signals': len(result.get('decisions', [])),
                        **summary,
                    })
        
        except Exception as e:
            self.logger.error(f"Task {task} failed: {e}")
            if self.telegram:
                self.telegram.notify_error(f"Scheduler.{task}", str(e))
    
    def run(self):
        """Main scheduler loop"""
        self.running = True
        self.logger.info("CryptoTrader Scheduler started")
        
        # Run initial cycle immediately
        for task in ['collect', 'sentiment', 'decide', 'execute']:
            self.run_task(task)
        
        while self.running:
            try:
                # Check each task
                for task in ['collect', 'sentiment', 'decide', 'execute']:
                    if self.should_run(task):
                        self.run_task(task)
                
                # Sleep 30 seconds between checks
                time.sleep(30)
            
            except KeyboardInterrupt:
                self.logger.info("Shutdown requested")
                break
            except Exception as e:
                self.logger.error(f"Scheduler error: {e}")
                time.sleep(60)  # Wait before retry
        
        self.logger.info("CryptoTrader Scheduler stopped")
    
    def stop(self):
        """Stop the scheduler"""
        self.running = False


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='CryptoTrader Scheduler')
    parser.add_argument('--config', default=None, help='Config file path')
    parser.add_argument('--collect-interval', type=int, default=600, help='Collection interval (seconds)')
    parser.add_argument('--sentiment-interval', type=int, default=1800, help='Sentiment interval (seconds)')
    parser.add_argument('--decide-interval', type=int, default=3600, help='Decision interval (seconds)')
    parser.add_argument('--execute-interval', type=int, default=300, help='Execution check interval (seconds)')
    
    args = parser.parse_args()
    
    scheduler = CryptoTraderScheduler()
    scheduler.setup(args.config)
    
    # Override intervals from CLI
    scheduler.intervals['collect'] = args.collect_interval
    scheduler.intervals['sentiment'] = args.sentiment_interval
    scheduler.intervals['decide'] = args.decide_interval
    scheduler.intervals['execute'] = args.execute_interval
    
    # Handle signals
    def signal_handler(sig, frame):
        scheduler.stop()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    scheduler.run()


if __name__ == '__main__':
    main()
