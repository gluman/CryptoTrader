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
from src.agents.email_notifier import EmailNotifier


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
            'sentiment': 1800,    # 30 minutes
            'decide': 900,       # 15 minutes
            'execute': 300,     # 5 minutes
            'hourly_report': 3600,  # 1 hour
        }
        
        self.last_run = {
            'collect': 0,
            'sentiment': 0,
            'decide': 0,
            'execute': 0,
            'hourly_report': 0,
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
        
        # Email
        self.email = None
        email_cfg = self.config.email
        if email_cfg.get('enabled') and email_cfg.get('sender'):
            self.email = EmailNotifier(
                smtp_host=email_cfg.get('smtp_host', 'smtp.gmail.com'),
                smtp_port=email_cfg.get('smtp_port', 587),
                sender=email_cfg.get('sender', ''),
                password=email_cfg.get('password', ''),
                recipients=email_cfg.get('recipients', []),
                logger=self.logger
            )
        
        self.logger.info("CryptoTrader Scheduler initialized")
    
    def should_run(self, task: str) -> bool:
        """Check if task should run based on interval"""
        if task not in self.intervals:
            return False
        elapsed = time.time() - self.last_run.get(task, 0)
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
            
            # Send hourly report
            if self.telegram and task == 'decide':
                self._send_hourly_report()
    
    def _send_hourly_report(self):
        """Send hourly summary to Telegram"""
        if not self.telegram:
            return
        
        # Gather stats from last hour
        total_errors = 0
        total_buys = 0
        total_sells = 0
        total_holds = 0
        ohlcv_count = 0
        news_count = 0
        avg_sentiment = 'N/A'
        
        # Get stats from agents
        if 'collect' in self.agents:
            try:
                collector = self.agents['collect']
                with self.db.get_session() as session:
                    from sqlalchemy import func
                    from src.core.database import OHLCVRaw, NewsRaw
                    from datetime import timedelta
                    
                    since = datetime.utcnow() - timedelta(hours=1)
                    ohlcv_count = session.query(func.count(OHLCVRaw.id)).filter(
                        OHLCVRaw.created_at >= since
                    ).scalar() or 0
                    news_count = session.query(func.count(NewsRaw.id)).filter(
                        NewsRaw.created_at >= since
                    ).scalar() or 0
            except Exception as e:
                self.logger.error(f"Failed to get stats: {e}")
        
        if 'decide' in self.agents:
            try:
                with self.db.get_session() as session:
                    from src.core.database import Signal
                    from datetime import timedelta
                    
                    since = datetime.utcnow() - timedelta(hours=1)
                    signals = session.query(Signal).filter(
                        Signal.created_at >= since
                    ).all()
                    
                    for s in signals:
                        if s.signal_type == 'BUY':
                            total_buys += 1
                        elif s.signal_type == 'SELL':
                            total_sells += 1
                        elif s.signal_type == 'HOLD':
                            total_holds += 1
            except Exception as e:
                self.logger.error(f"Failed to get signals: {e}")
        
        if 'sentiment' in self.agents:
            try:
                with self.db.get_session() as session:
                    from src.core.database import NewsRaw
                    from sqlalchemy import func
                    from datetime import timedelta
                    
                    since = datetime.utcnow() - timedelta(hours=1)
                    avg = session.query(func.avg(NewsRaw.sentiment_score)).filter(
                        NewsRaw.created_at >= since,
                        NewsRaw.sentiment_score.isnot(None)
                    ).scalar()
                    if avg:
                        avg_sentiment = round(avg, 2)
            except Exception as e:
                self.logger.error(f"Failed to get sentiment: {e}")
        
        summary = {
            'ohlcv_records': ohlcv_count,
            'news_records': news_count,
            'avg_sentiment': avg_sentiment,
            'buys': total_buys,
            'sells': total_sells,
            'holds': total_holds,
            'trades_executed': total_buys + total_sells,
            'errors': total_errors,
        }
        
        self.telegram.notify_hourly_summary(summary)
            if self.email:
                self.email.notify_hourly_summary(summary)
        
        except Exception as e:
            self.logger.error(f"Task {task} failed: {e}")
            if self.telegram:
                self.telegram.notify_error(f"Scheduler.{task}", str(e))
            if self.email:
                self.email.notify_error(f"Scheduler.{task}", str(e))
    
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
