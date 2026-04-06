import pytest
from src.core.config import Config


class TestConfig:
    """Tests for Config class"""
    
    def test_singleton(self):
        """Config should be singleton"""
        config1 = Config.load()
        config2 = Config.load()
        assert config1 is config2
    
    def test_load_from_env(self, config):
        """Config should load secrets from .env"""
        assert config.binance.get('api_key') is not None
        assert len(config.binance.get('api_key', '')) > 0
    
    def test_get_nested(self, config):
        """Config.get() should support dot notation"""
        assert config.get('openrouter.model') == 'meta-llama/llama-3.3-70b-instruct:free'
        assert config.get('agents.trading_decision.min_confidence') == 0.6
    
    def test_get_default(self, config):
        """Config.get() should return default for missing keys"""
        assert config.get('nonexistent.key', 'default') == 'default'
    
    def test_timeframes(self, config):
        """Config should have timeframes"""
        tfs = config.timeframes
        assert '1h' in tfs
        assert '4h' in tfs
        assert '1d' in tfs
    
    def test_rss_feeds(self, config):
        """Config should have RSS feeds"""
        feeds = config.rss_feeds
        assert len(feeds) > 0
        assert any(f['name'] == 'CoinDesk' for f in feeds)
    
    def test_risk_config(self, config):
        """Config should have risk parameters"""
        risk = config.agents.get('risk', {})
        assert 'max_position_percent' in risk
        assert 'default_stop_loss_percent' in risk
        assert 'default_take_profit_percent' in risk
    
    def test_css_indicator_config(self, config):
        """Config should have CSS indicator settings"""
        css = config.css_indicator
        assert 'level_trade' in css
        assert 'sma_period' in css
