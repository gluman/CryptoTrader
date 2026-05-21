#!/usr/bin/env python3
"""
ScalpingAgent — runs scalping strategy on 1m/5m timeframe
Fast trades, tight stops, high frequency
"""

import sys
import os
# Add project root to path
sys.path.insert(0, '/home/andy')

from src.core.config import Config
from src.core.database import DatabaseManager
from src.agents.sentiment_agent import SentimentAgent
from cryptotrader_strategies import ScalpingStrategy


class ScalpingAgent:
    def __init__(self):
        self.config = Config.load()
        self.logger = self._setup_logger()
        self.db = DatabaseManager(self.config.postgresql, self.logger)
        self.strategy = ScalpingStrategy()
        self.symbols = self.strategy.get_default_symbols()

    def _setup_logger(self):
        import logging
        logger = logging.getLogger('scalping_agent')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            ))
            logger.addHandler(handler)
        return logger

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> object:
        """Get OHLCV data from DB"""
        with self.db.get_session() as session:
            from src.core.database import OHLCVRaw
            records = session.query(OHLCVRaw).filter(
                OHLCVRaw.exchange == 'bybit',
                OHLCVRaw.symbol == symbol,
                OHLCVRaw.timeframe == timeframe
            ).order_by(OHLCVRaw.timestamp.desc()).limit(limit).all()

            if not records:
                return None

            import pandas as pd
            data = [{
                'timestamp': r.timestamp,
                'open': float(r.open),
                'high': float(r.high),
                'low': float(r.low),
                'close': float(r.close),
                'volume': float(r.volume),
            } for r in reversed(records)]

            return pd.DataFrame(data).set_index('timestamp') if data else None

    def run(self):
        self.logger.info(f"ScalpingAgent starting for {self.symbols}")

        for symbol in self.symbols:
            df_1m = self.get_ohlcv(symbol, '1m', limit=50)
            df_5m = self.get_ohlcv(symbol, '5m', limit=100)

            if df_5m is None:
                self.logger.warning(f"No data for {symbol}, skipping")
                continue

            signal = self.strategy.analyze(
                df_1m=df_1m,
                df_5m=df_5m,
                df_15m=None,
                df_1h=None,
                df_4h=None,
                df_1d=None,
                indicators={}
            )

            self.logger.info(f"{symbol}: {signal['action']} "
                           f"(confidence={signal.get('confidence', 0):.3f}) "
                           f"- {signal.get('reasoning', '')}")

            # If signal is actionable, save to DB
            if signal['action'] != 'HOLD' and signal.get('confidence', 0) > 0:
                self._save_signal(symbol, signal)

        self.logger.info("ScalpingAgent finished")

    def _save_signal(self, symbol: str, signal: dict):
        """Save signal to database for execution"""
        with self.db.get_session() as session:
            from src.core.database import StrategySignal
            from datetime import datetime, timezone

            # Convert numpy types to native Python types
            def to_native(val):
                if val is None:
                    return None
                if hasattr(val, 'item'):  # numpy type
                    return val.item()
                return val

            s = StrategySignal(
                symbol=symbol,
                exchange='bybit',
                strategy=self.strategy.name,
                action=signal['action'],
                confidence=float(to_native(signal.get('confidence', 0))),
                entry_price=float(to_native(signal.get('entry_price'))) if signal.get('entry_price') else None,
                stop_loss=float(to_native(signal.get('stop_loss'))) if signal.get('stop_loss') else None,
                take_profit=float(to_native(signal.get('take_profit'))) if signal.get('take_profit') else None,
                timeframes=','.join(signal.get('timeframes', [])),
                reasoning=signal.get('reasoning', ''),
                status='pending'
            )
            session.add(s)
            session.commit()
            self.logger.info(f"Saved signal: {symbol} {signal['action']}")

            # Also add to legacy signals table for ExecutionAgent
            from src.core.database import Signal
            sig = Signal(
                symbol=symbol,
                exchange='bybit',
                market_type='linear',
                timeframe=signal.get('timeframes', ['1m'])[0] if signal.get('timeframes') else '1m',
                timestamp=datetime.now(timezone.utc),
                signal_type=signal['action'],  # BUY or SELL
                strength=float(to_native(signal.get('confidence', 0))),
                confidence=float(to_native(signal.get('confidence', 0))),
                reasoning=signal.get('reasoning', ''),
                status='PENDING'
            )
            session.add(sig)
            session.commit()
            self.logger.info(f"Added legacy signal for ExecutionAgent: {symbol} {signal['action']}")


if __name__ == '__main__':
    agent = ScalpingAgent()
    agent.run()
