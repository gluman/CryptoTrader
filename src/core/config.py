import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from dotenv import load_dotenv


class Config:
    """Configuration loader for CryptoTrader with .env support"""
    
    _instance = None
    
    def __new__(cls, config_path: Optional[str] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load(config_path)
        return cls._instance
    
    def _load(self, config_path: Optional[str] = None):
        # Load .env from project root
        project_root = Path(__file__).parent.parent.parent
        env_path = project_root / '.env'
        load_dotenv(env_path)
        
        if config_path is None:
            config_path = project_root / "config" / "settings.yaml"
        
        with open(config_path, 'r', encoding='utf-8') as f:
            self._data = yaml.safe_load(f)
        
        self._inject_secrets()
    
    def _inject_secrets(self):
        """Load secrets from environment variables into config"""
        # Binance
        self._data.setdefault('binance', {})
        self._data['binance']['api_key'] = os.getenv('BINANCE_API_KEY', '')
        self._data['binance']['api_secret'] = os.getenv('BINANCE_API_SECRET', '')
        self._data['binance']['testnet'] = os.getenv('BINANCE_TESTNET', 'false').lower() == 'true'
        
        # Bybit
        self._data.setdefault('bybit', {})
        self._data['bybit']['api_key'] = os.getenv('BYBIT_API_KEY', '')
        self._data['bybit']['api_secret'] = os.getenv('BYBIT_API_SECRET', '')
        self._data['bybit']['testnet'] = os.getenv('BYBIT_TESTNET', 'false').lower() == 'true'
        
        # Bitfinex
        self._data.setdefault('bitfinex', {})
        self._data['bitfinex']['api_key'] = os.getenv('BITFINEX_API_KEY', '')
        self._data['bitfinex']['api_secret'] = os.getenv('BITFINEX_API_SECRET', '')
        self._data['bitfinex']['testnet'] = os.getenv('BITFINEX_TESTNET', 'false').lower() == 'true'
        
        # CoinEx
        self._data.setdefault('coinex', {})
        self._data['coinex']['access_id'] = os.getenv('COINEX_ACCESS_ID', '')
        self._data['coinex']['secret_key'] = os.getenv('COINEX_SECRET_KEY', '')
        
        # Telegram
        self._data.setdefault('telegram', {})
        self._data['telegram']['bot_token'] = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self._data['telegram']['chat_id'] = os.getenv('TELEGRAM_CHAT_ID', '')
        
        # CryptoRank
        self._data.setdefault('cryptorank', {})
        self._data['cryptorank']['api_key'] = os.getenv('CRYPTORANK_API_KEY', '')
        
        # CoinDesk
        self._data.setdefault('coindesk', {})
        self._data['coindesk']['api_key'] = os.getenv('COINDESK_API_KEY', '')
        
        # OpenRouter
        self._data.setdefault('openrouter', {})
        self._data['openrouter']['api_key'] = os.getenv('OPENROUTER_API_KEY', '')
        
        # Ollama
        self._data.setdefault('ollama', {})
        self._data['ollama']['base_url'] = os.getenv('OLLAMA_BASE_URL', 'http://192.168.0.94:11434')
        
        # PostgreSQL
        self._data.setdefault('postgresql', {})
        self._data['postgresql']['host'] = os.getenv('POSTGRES_HOST', '192.168.0.194')
        self._data['postgresql']['port'] = int(os.getenv('POSTGRES_PORT', '5432'))
        self._data['postgresql']['database'] = os.getenv('POSTGRES_DB', 'cryptotrader')
        self._data['postgresql']['username'] = os.getenv('POSTGRES_USER', 'cryptotrader')
        self._data['postgresql']['password'] = os.getenv('POSTGRES_PASSWORD', '')
        
        # RAGFlow
        self._data.setdefault('ragflow', {})
        self._data['ragflow']['base_url'] = os.getenv('RAGFLOW_BASE_URL', 'http://192.168.0.186:9380')
        self._data['ragflow']['api_key'] = os.getenv('RAGFLOW_API_KEY', '')
        
        # AnythingLLM
        self._data.setdefault('anythingllm', {})
        self._data['anythingllm']['base_url'] = os.getenv('ANYTHINGLLM_BASE_URL', 'http://192.168.0.133:3001')
        self._data['anythingllm']['api_key'] = os.getenv('ANYTHINGLLM_API_KEY', '')
    
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
    def bitfinex(self) -> Dict:
        return self.get('bitfinex', {})
    
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
        Config._instance = None
        return Config.load()
