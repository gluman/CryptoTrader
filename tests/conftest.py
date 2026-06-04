import pytest
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from decimal import Decimal
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import Config
from src.core.database import (
    DatabaseManager, OHLCVRaw, NewsRaw, Signal, 
    Decision, Trade, Position, SelectedSymbol
)


@pytest.fixture
def config():
    """Load test configuration"""
    return Config.load()


@pytest.fixture
def mock_logger():
    """Create mock logger"""
    logger = MagicMock()
    logger.info = MagicMock()
    logger.error = MagicMock()
    logger.warning = MagicMock()
    logger.debug = MagicMock()
    return logger


@pytest.fixture
def sample_ohlcv_data():
    """Generate sample OHLCV data for testing"""
    import pandas as pd
    import numpy as np
    
    np.random.seed(42)
    dates = pd.date_range('2026-01-01', periods=100, freq='1h')
    
    close = 100 + np.cumsum(np.random.randn(100) * 0.5)
    data = pd.DataFrame({
        'open': close + np.random.randn(100) * 0.1,
        'high': close + np.abs(np.random.randn(100) * 0.3),
        'low': close - np.abs(np.random.randn(100) * 0.3),
        'close': close,
        'volume': np.random.randint(1000, 10000, 100).astype(float),
    }, index=dates)
    
    return data


@pytest.fixture
def sample_signal():
    """Create sample signal dict"""
    return {
        'symbol': 'BTCUSDT',
        'exchange': 'binance',
        'timeframe': '1h',
        'signal_type': 'BUY',
        'strength': 0.75,
        'css_value': 0.25,
        'rsi_14': 45.0,
        'macd': 0.001,
        'atr_14': 150.0,
        'price': 65000.0,
        'sentiment_score': 0.3,
        'confidence': 0.75,
        'reasoning': 'CSS crossed up, bullish trend',
        'status': 'PENDING',
    }


@pytest.fixture
def sample_position():
    """Create sample position"""
    return {
        'symbol': 'BTCUSDT',
        'exchange': 'binance',
        'entry_price': 65000.0,
        'quantity': 0.001,
        'cost_usdt': 65.0,
        'stop_loss': 63700.0,
        'take_profit': 67600.0,
    }
