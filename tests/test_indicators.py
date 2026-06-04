import pytest
import numpy as np
import pandas as pd
from src.agents.trading_agent import TradingDecisionAgent


class TestIndicators:
    """Tests for technical indicator calculations"""
    
    def _create_agent(self, mock_logger):
        """Create TradingDecisionAgent with mocked dependencies"""
        from src.core.config import Config
        config = Config.load()
        
        db_mock = type('DB', (), {'get_session': lambda self: None})()
        sentiment_mock = type('Sentiment', (), {
            'get_aggregated_sentiment': lambda self, hours=24: {
                'avg_sentiment': 0.0, 'bullish_ratio': 0.5,
                'news_count': 0, 'sample_titles': []
            }
        })()
        
        agent = TradingDecisionAgent(config, mock_logger, db_mock, sentiment_mock)
        return agent
    
    def test_calculate_indicators_basic(self, sample_ohlcv_data, mock_logger):
        """Test basic indicator calculation"""
        agent = self._create_agent(mock_logger)
        indicators = agent.calculate_indicators(sample_ohlcv_data)
        
        assert 'price' in indicators
        assert 'sma_20' in indicators
        assert 'sma_50' in indicators
        assert 'rsi_14' in indicators
        assert 'macd' in indicators
        assert 'atr_14' in indicators
        assert 'css_value' in indicators
        assert 'volume_ratio' in indicators
    
    def test_rsi_range(self, sample_ohlcv_data, mock_logger):
        """RSI should be between 0 and 100"""
        agent = self._create_agent(mock_logger)
        indicators = agent.calculate_indicators(sample_ohlcv_data)
        
        rsi = indicators['rsi_14']
        assert 0 <= rsi <= 100
    
    def test_sma_50_below_sma_20(self, sample_ohlcv_data, mock_logger):
        """SMA50 should be close to SMA20 (not drastically different)"""
        agent = self._create_agent(mock_logger)
        indicators = agent.calculate_indicators(sample_ohlcv_data)
        
        diff_pct = abs(indicators['sma_20'] - indicators['sma_50']) / indicators['sma_20'] * 100
        assert diff_pct < 10  # Less than 10% difference
    
    def test_bollinger_bands_order(self, sample_ohlcv_data, mock_logger):
        """BB upper > middle > lower"""
        agent = self._create_agent(mock_logger)
        indicators = agent.calculate_indicators(sample_ohlcv_data)
        
        assert indicators['bb_upper'] > indicators['bb_middle']
        assert indicators['bb_middle'] > indicators['bb_lower']
    
    def test_atr_positive(self, sample_ohlcv_data, mock_logger):
        """ATR should be positive"""
        agent = self._create_agent(mock_logger)
        indicators = agent.calculate_indicators(sample_ohlcv_data)
        
        assert indicators['atr_14'] > 0
    
    def test_css_calculation(self, sample_ohlcv_data, mock_logger):
        """CSS should have trend and cross indicators"""
        agent = self._create_agent(mock_logger)
        indicators = agent.calculate_indicators(sample_ohlcv_data)
        
        assert 'css_value' in indicators
        assert 'css_prior' in indicators
        assert 'css_trend' in indicators
        assert indicators['css_trend'] in ['BULLISH', 'BEARISH', 'NEUTRAL']
        assert isinstance(indicators['css_cross_up'], bool)
        assert isinstance(indicators['css_cross_down'], bool)
    
    def test_css_trending_up_data(self, mock_logger):
        """CSS should detect bullish trend in uptrending data"""
        agent = self._create_agent(mock_logger)
        
        # Create uptrending data
        dates = pd.date_range('2026-01-01', periods=100, freq='1h')
        close = pd.Series(np.linspace(100, 120, 100), index=dates)
        
        df = pd.DataFrame({
            'open': close - 0.1,
            'high': close + 0.2,
            'low': close - 0.2,
            'close': close,
            'volume': np.ones(100) * 5000,
        }, index=dates)
        
        indicators = agent.calculate_indicators(df)
        assert indicators['css_trend'] == 'BULLISH'
    
    def test_empty_dataframe(self, mock_logger):
        """Should handle empty DataFrame gracefully"""
        agent = self._create_agent(mock_logger)
        indicators = agent.calculate_indicators(pd.DataFrame())
        assert indicators == {}
    
    def test_short_dataframe(self, mock_logger):
        """Should handle DataFrame with insufficient bars"""
        agent = self._create_agent(mock_logger)
        
        dates = pd.date_range('2026-01-01', periods=10, freq='1h')
        df = pd.DataFrame({
            'open': [100] * 10,
            'high': [101] * 10,
            'low': [99] * 10,
            'close': [100.5] * 10,
            'volume': [5000] * 10,
        }, index=dates)
        
        indicators = agent.calculate_indicators(df)
        assert indicators == {}


class TestCSSIndicator:
    """Specific tests for CSS calculation"""
    
    def _create_agent(self, mock_logger):
        from src.core.config import Config
        config = Config.load()
        db_mock = type('DB', (), {'get_session': lambda self: None})()
        sentiment_mock = type('Sentiment', (), {
            'get_aggregated_sentiment': lambda self, hours=24: {}
        })()
        return TradingDecisionAgent(config, mock_logger, db_mock, sentiment_mock)
    
    def test_css_flat_market(self, mock_logger):
        """CSS should be near zero in flat market"""
        agent = self._create_agent(mock_logger)
        
        dates = pd.date_range('2026-01-01', periods=50, freq='1h')
        close = pd.Series([100.0] * 50, index=dates)
        atr = 1.0
        
        result = agent._calculate_css(close, atr)
        assert abs(result['css']) < 0.01
        assert result['trend'] == 'NEUTRAL'
    
    def test_css_strong_uptrend(self, mock_logger):
        """CSS should be positive in strong uptrend"""
        agent = self._create_agent(mock_logger)
        
        dates = pd.date_range('2026-01-01', periods=50, freq='1h')
        close = pd.Series(np.linspace(100, 110, 50), index=dates)
        atr = 0.5
        
        result = agent._calculate_css(close, atr)
        assert result['css'] > 0
        assert result['trend'] == 'BULLISH'
    
    def test_css_strong_downtrend(self, mock_logger):
        """CSS should be negative in strong downtrend"""
        agent = self._create_agent(mock_logger)
        
        dates = pd.date_range('2026-01-01', periods=50, freq='1h')
        close = pd.Series(np.linspace(110, 100, 50), index=dates)
        atr = 0.5
        
        result = agent._calculate_css(close, atr)
        assert result['css'] < 0
        assert result['trend'] == 'BEARISH'
