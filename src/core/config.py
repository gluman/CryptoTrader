import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional

class Config:
    """Configuration loader for CryptoTrader"""
    
    _instance = None
    
    def __new__(cls, config_path: Optional[str] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load(config_path)
        return cls._instance
    
    def _load(self, config_path: Optional[str] = None):
        if config_path is None:
            # Default path relative to project root
            project_root = Path(__file__).parent.parent.parent
            config_path = project_root / "config" / "settings.yaml"
        
        with open(config_path, 'r', encoding='utf-8') as f:
            self._data = yaml.safe_load(f)
    
    @classmethod
    def load(cls, config_path: Optional[str] = None) -> 'Config':
        return cls(config_path)
    
    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split('.')
        value = self._data
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
        return value if value is not None else default
    
    # Convenience properties
    @property
    def binance(self) -> Dict:
        return self.get('binance', {})
    
    @property
    def bybit(self) -> Dict:
        return self.get('bybit', {})
    
    @property
    def postgresql(self) -> Dict:
        return self.get('postgresql', {})
    
    @property
    def openrouter(self) -> Dict:
        return self.get('openrouter', {})
    
    @property
    def ragflow(self) -> Dict:
        return self.get('ragflow', {})
    
    @property
    def telegram(self) -> Dict:
        return self.get('telegram', {})
    
    @property
    def cryptorank(self) -> Dict:
        return self.get('cryptorank', {})
    
    @property
    def coindesk(self) -> Dict:
        return self.get('coindesk', {})
    
    @property
    def rss_feeds(self) -> list:
        return self.get('rss_feeds', [])
    
    @property
    def timeframes(self) -> list:
        return self.get('timeframes', ['1m', '5m', '15m', '1h', '4h', '1d'])
    
    @property
    def selection_criteria(self) -> Dict:
        return self.get('selection_criteria', {})
    
    @property
    def css_indicator(self) -> Dict:
        return self.get('css_indicator', {})
    
    @property
    def agents(self) -> Dict:
        return self.get('agents', {})
    
    @property
    def logging(self) -> Dict:
        return self.get('logging', {})
    
    @property
    def export(self) -> Dict:
        return self.get('export', {})
    
    @property
    def anythingllm(self) -> Dict:
        return self.get('anythingllm', {})

    def reload(self):
        """Reload configuration from file"""
        self._instance = None
        return Config.load()
