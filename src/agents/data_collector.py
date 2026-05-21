import requests
import feedparser
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from .base import BaseAgent
from ..gateways import BinanceAPI, BybitAPI
from ..core.config import Config
from ..core.database import DatabaseManager, OHLCVRaw, NewsRaw

class DataCollectorAgent(BaseAgent):
    """Collects OHLCV data, news, and market metrics"""
    
    def __init__(self, config: Config, logger: logging.Logger, db: DatabaseManager):
        super().__init__('DataCollector', logger)
        self.config = config
        self.db = db
        self.binance = BinanceAPI(
            api_key=config.binance['api_key'],
            api_secret=config.binance['api_secret'],
            testnet=config.binance.get('testnet', False),
            logger=logger
        )
        self.bybit = BybitAPI(
            api_key=config.bybit['api_key'],
            api_secret=config.bybit['api_secret'],
            testnet=config.bybit.get('testnet', False),
            logger=logger
        )
    
    def fetch_binance_klines(self, symbol: str, interval: str, limit: int = 500) -> List[Dict]:
        """Fetch OHLCV from Binance"""
        try:
            raw = self.binance.get_klines(symbol, interval, limit)
            return [
                {
                    'exchange': 'binance',
                    'symbol': symbol,
                    'timeframe': interval,
                    'timestamp': datetime.utcfromtimestamp(k[0] / 1000),
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5]),
                    'quote_volume': float(k[7]),
                    'trades_count': int(k[8]),
                }
                for k in raw
            ]
        except Exception as e:
            self.log('error', f"Failed to fetch Binance klines {symbol}: {e}")
            return []
    
    def fetch_bybit_klines(self, symbol: str, interval: str, limit: int = 200) -> List[Dict]:
        """Fetch OHLCV from Bybit"""
        try:
            resp = self.bybit.get_kline(symbol, interval, limit=limit)
            klines = resp['result']['list']
            return [
                {
                    'exchange': 'bybit',
                    'symbol': symbol,
                    'timeframe': interval,
                    'timestamp': datetime.utcfromtimestamp(int(k[0]) / 1000),
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5]),
                    'quote_volume': float(k[6]),
                    'trades_count': None,
                }
                for k in klines
            ]
        except Exception as e:
            self.log('error', f"Failed to fetch Bybit klines {symbol}: {e}")
            return []
    
    def fetch_binance_tickers(self) -> List[Dict]:
        """Fetch all tickers from Binance for symbol selection"""
        try:
            return self.binance.get_all_tickers()
        except Exception as e:
            self.log('error', f"Failed to fetch Binance tickers: {e}")
            return []
    
    def select_symbols(self) -> List[str]:
        criteria = self.config.selection_criteria
        min_volume = criteria.get('min_volume_24h', 50_000_000)
        min_change = criteria.get('min_change_1h', 2.0)
        quote = criteria.get('quote_currency', 'USDT')
        
        tickers = self.fetch_binance_tickers()
        selected = []
        
        for t in tickers[:500]:
            symbol = t.get('symbol', '')
            if not symbol.endswith(quote):
                continue
            try:
                volume = float(t.get('quoteVolume', 0))
                change = abs(float(t.get('priceChangePercent', 0)))
                if volume >= min_volume and change >= min_change:
                    selected.append(symbol)
            except (ValueError, TypeError):
                continue
            if len(selected) >= 15:
                break
        
        self.log('info', f"Selected {len(selected)} symbols from {len(tickers)} tickers")
        # Ensure BTCUSDT and ETHUSDT are always included for trading
        for must_have in ['ETHUSDT', 'BTCUSDT']:
            if must_have not in selected:
                selected.insert(0, must_have)
        return selected[:20]
    
    def save_ohlcv_to_db(self, data: List[Dict]):
        """Save OHLCV data to PostgreSQL"""
        if not data:
            return 0
        
        with self.db.get_session() as session:
            from sqlalchemy.dialects.postgresql import insert
            stmt = insert(OHLCVRaw).values(data)
            stmt = stmt.on_conflict_do_update(
                index_elements=['exchange', 'symbol', 'timeframe', 'timestamp'],
                set_={
                    'open': stmt.excluded.open,
                    'high': stmt.excluded.high,
                    'low': stmt.excluded.low,
                    'close': stmt.excluded.close,
                    'volume': stmt.excluded.volume,
                }
            )
            result = session.execute(stmt)
            return result.rowcount
    
    def fetch_rss_news(self, timeout_seconds: int = 5) -> List[Dict]:
        """Parse RSS news feeds with timeout"""
        import socket
        all_news = []
        feeds = self.config.rss_feeds
        
        for feed in feeds:
            if not feed.get('enabled', True):
                continue
            
            try:
                self.log('info', f"Fetching RSS: {feed['name']}...")
                socket.setdefaulttimeout(timeout_seconds)
                parsed = feedparser.parse(feed['url'])
                
                if not parsed.entries:
                    self.log('warning', f"No entries from {feed['name']}")
                    continue
                    
                for entry in parsed.entries[:10]:
                    news_item = {
                        'source': feed['name'],
                        'title': entry.get('title', ''),
                        'url': entry.get('link', ''),
                        'published_at': datetime.utcnow(),
                        'summary': entry.get('summary', '')[:500],
                        'language': feed.get('language', 'en'),
                    }
                    all_news.append(news_item)
                    
                self.log('info', f"Got {len(parsed.entries)} entries from {feed['name']}")
                
            except Exception as e:
                self.log('error', f"RSS parse failed {feed['name']}: {e}")
        
        return all_news
    
    def save_news_to_db(self, news: List[Dict]):
        """Save news to PostgreSQL"""
        saved = 0
        with self.db.get_session() as session:
            for item in news:
                exists = session.query(NewsRaw).filter_by(url=item['url']).first()
                if not exists:
                    session.add(NewsRaw(**item))
                    saved += 1
        return saved
    
    def fetch_cryptorank_global(self) -> Dict:
        """Fetch global market data from CryptoRank"""
        try:
            headers = {'X-Api-Key': self.config.cryptorank['api_key']}
            resp = requests.get(
                f"{self.config.cryptorank['base_url']}/global",
                headers=headers, timeout=30
            )
            return resp.json().get('data', {})
        except Exception as e:
            self.log('error', f"CryptoRank global fetch failed: {e}")
            return {}
    
    def run_once(self, timeout_seconds: int = 60) -> Dict[str, Any]:
        """One data collection cycle with timeout protection"""
        import signal
        import threading
        
        class TimeoutError(Exception):
            pass
        
        def timeout_handler():
            raise TimeoutError("Data collection timed out")
        
        self.log('info', "Starting data collection cycle...")
        
        stats = {
            'ohlcv_records': 0,
            'news_records': 0,
            'symbols_selected': 0,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        # 1. Select symbols (every hour or first run) - with timeout
        self.log('info', "Selecting symbols...")
        try:
            symbols = self.select_symbols()
            self.log('info', f"Selected {len(symbols)} symbols: {symbols[:5]}...")
        except Exception as e:
            self.log('error', f"Symbol selection failed: {e}")
            symbols = []
        
        stats['symbols_selected'] = len(symbols)
        
        # 2. Collect OHLCV for each symbol — Bybit source
        # 1m/5m for scalping, higher timeframes for analysis
        timeframes_bybit = {'1m': '1', '5m': '5', '1h': '60', '4h': '240'}

        # Scalping targets: BTCUSDT, ETHUSDT, SOLUSDT
        scalping_symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']

        # Priority: collect 1m scalping data first, then general timeframes
        # Scalping symbols get 1m data
        for symbol in scalping_symbols:
            for tf, bybit_interval in timeframes_bybit.items():
                self.log('info', f"Fetching {symbol} {tf} from Bybit...")
                try:
                    data_bybit = self.fetch_bybit_klines(symbol, bybit_interval, limit=200)
                    count = self.save_ohlcv_to_db(data_bybit)
                    stats['ohlcv_records'] += count
                    self.log('info', f"Saved {count} records for {symbol} {tf}")
                except Exception as e:
                    self.log('error', f"Failed to fetch {symbol} {tf}: {e}")

        # General collection for other symbols (1h, 4h only)
        timeframes_general = {'1h': '60', '4h': '240'}
        for symbol in symbols[:5]:
            if symbol in scalping_symbols:
                continue  # Already collected above
            for tf, bybit_interval in timeframes_general.items():
                self.log('info', f"Fetching {symbol} {tf} from Bybit...")
                try:
                    data_bybit = self.fetch_bybit_klines(symbol, bybit_interval, limit=100)
                    count = self.save_ohlcv_to_db(data_bybit)
                    stats['ohlcv_records'] += count
                    self.log('info', f"Saved {count} records for {symbol} {tf}")
                except Exception as e:
                    self.log('error', f"Failed to fetch {symbol} {tf}: {e}")
        
        # 3. Collect RSS news
        self.log('info', "Fetching RSS news...")
        try:
            news = self.fetch_rss_news()
            stats['news_records'] = self.save_news_to_db(news)
            self.log('info', f"Saved {stats['news_records']} news records")
        except Exception as e:
            self.log('error', f"RSS news collection failed: {e}")
            stats['news_records'] = 0
        
        self.last_run = datetime.utcnow()
        self.log('info', f"Collection complete: {stats['ohlcv_records']} OHLCV, {stats['news_records']} news")
        
        return stats
