import pytest
from src.agents.trading_agent import TradingDecisionAgent


class TestPromptBuilding:
    """Tests for LLM prompt construction"""
    
    def _create_agent(self, mock_logger):
        from src.core.config import Config
        config = Config.load()
        db_mock = type('DB', (), {'get_session': lambda self: None})()
        sentiment_mock = type('Sentiment', (), {
            'get_aggregated_sentiment': lambda self, hours=24: {}
        })()
        return TradingDecisionAgent(config, mock_logger, db_mock, sentiment_mock)
    
    def test_prompt_contains_symbol(self, mock_logger):
        """Prompt should contain the symbol"""
        agent = self._create_agent(mock_logger)
        
        indicators = {'price': 65000, 'sma_20': 64500, 'sma_50': 64000,
                      'rsi_14': 45, 'macd': 0.001, 'macd_signal': 0.0005,
                      'atr_14': 150, 'bb_upper': 66000, 'bb_lower': 63000,
                      'bb_middle': 64500, 'css_value': 0.25, 'css_trend': 'BULLISH',
                      'css_cross_up': True, 'css_cross_down': False,
                      'volume_ratio': 1.5}
        sentiment = {'avg_sentiment': 0.3, 'bullish_ratio': 0.6, 'news_count': 10}
        
        prompt = agent.build_prompt('BTCUSDT', indicators, sentiment, [], [], '')
        
        assert 'BTCUSDT' in prompt
    
    def test_prompt_contains_indicators(self, mock_logger):
        """Prompt should contain indicator values"""
        agent = self._create_agent(mock_logger)
        
        indicators = {'price': 65000, 'sma_20': 64500, 'sma_50': 64000,
                      'rsi_14': 45, 'macd': 0.001, 'macd_signal': 0.0005,
                      'atr_14': 150, 'bb_upper': 66000, 'bb_lower': 63000,
                      'bb_middle': 64500, 'css_value': 0.25, 'css_trend': 'BULLISH',
                      'css_cross_up': True, 'css_cross_down': False,
                      'volume_ratio': 1.5}
        sentiment = {'avg_sentiment': 0.3, 'bullish_ratio': 0.6, 'news_count': 10}
        
        prompt = agent.build_prompt('BTCUSDT', indicators, sentiment, [], [], '')
        
        assert 'RSI' in prompt
        assert 'MACD' in prompt
        assert 'CSS' in prompt
    
    def test_prompt_contains_rag_context(self, mock_logger):
        """Prompt should include RAG context when provided"""
        agent = self._create_agent(mock_logger)
        
        indicators = {'price': 65000, 'sma_20': 64500, 'sma_50': 64000,
                      'rsi_14': 45, 'macd': 0.001, 'macd_signal': 0.0005,
                      'atr_14': 150, 'bb_upper': 66000, 'bb_lower': 63000,
                      'bb_middle': 64500, 'css_value': 0.25, 'css_trend': 'BULLISH',
                      'css_cross_up': True, 'css_cross_down': False,
                      'volume_ratio': 1.5}
        sentiment = {'avg_sentiment': 0.3, 'bullish_ratio': 0.6, 'news_count': 10}
        rag_context = "Expert says BTC is bullish due to ETF inflows"
        
        prompt = agent.build_prompt('BTCUSDT', indicators, sentiment, [], [], rag_context)
        
        assert 'RAG' in prompt
        assert 'ETF inflows' in prompt
    
    def test_prompt_contains_open_position(self, mock_logger):
        """Prompt should show open position info"""
        agent = self._create_agent(mock_logger)
        
        indicators = {'price': 66000, 'sma_20': 64500, 'sma_50': 64000,
                      'rsi_14': 55, 'macd': 0.002, 'macd_signal': 0.001,
                      'atr_14': 150, 'bb_upper': 67000, 'bb_lower': 63000,
                      'bb_middle': 65000, 'css_value': 0.3, 'css_trend': 'BULLISH',
                      'css_cross_up': False, 'css_cross_down': False,
                      'volume_ratio': 1.2}
        sentiment = {'avg_sentiment': 0.2, 'bullish_ratio': 0.55, 'news_count': 8}
        positions = [{
            'id': 1, 'entry_price': 65000, 'quantity': 0.001,
            'stop_loss': 63700, 'take_profit': 67600,
            'unrealized_pnl_pct': 1.54, 'opened_at': '2026-03-31T12:00:00',
        }]
        
        prompt = agent.build_prompt('BTCUSDT', indicators, sentiment, [], positions, '')
        
        assert 'Open Position' in prompt
        assert '65000' in prompt
    
    def test_prompt_contains_rules(self, mock_logger):
        """Prompt should contain trading rules"""
        agent = self._create_agent(mock_logger)
        
        indicators = {'price': 65000, 'sma_20': 64500, 'sma_50': 64000,
                      'rsi_14': 45, 'macd': 0.001, 'macd_signal': 0.0005,
                      'atr_14': 150, 'bb_upper': 66000, 'bb_lower': 63000,
                      'bb_middle': 64500, 'css_value': 0.25, 'css_trend': 'BULLISH',
                      'css_cross_up': True, 'css_cross_down': False,
                      'volume_ratio': 1.5}
        sentiment = {'avg_sentiment': 0.3, 'bullish_ratio': 0.6, 'news_count': 10}
        
        prompt = agent.build_prompt('BTCUSDT', indicators, sentiment, [], [], '')
        
        assert 'BUY when' in prompt
        assert 'SELL when' in prompt
        assert 'HOLD when' in prompt
        assert 'JSON' in prompt
