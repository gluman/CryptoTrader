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
        # Map string timeframe → Bybit API numeric param
        tf_to_api = {'1m': '1', '3m': '3', '5m': '5', '15m': '15',
                     '30m': '30', '1h': '60', '4h': '240', '1d': 'D', '1w': 'W'}
        api_interval = tf_to_api.get(interval, interval)

        try:
            resp = self.bybit.get_kline(symbol, api_interval, limit=limit)
            klines = resp['result']['list']
            return [
                {
                    'exchange': 'bybit',
                    'symbol': symbol,
                    'timeframe': interval,  # Store string tf name directly
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

        # Use Bybit for symbol selection (primary exchange)
        tickers = self.fetch_bybit_tickers()
        selected = []

        for t in tickers[:500]:
            symbol = t.get('symbol', '')
            if not symbol.endswith(quote):
                continue
            try:
                volume = float(t.get('turnover24h', 0))
                change = abs(float(t.get('price24hPcnt', 0)) * 100)
                if volume >= min_volume and change >= min_change:
                    selected.append(symbol)
            except (ValueError, TypeError):
                continue
            if len(selected) >= 15:
                break

        self.log('info', f"Selected {len(selected)} symbols from {len(tickers)} Bybit tickers")
        # Ensure BTCUSDT and ETHUSDT are always included for trading
        for must_have in ['BTCUSDT', 'ETHUSDT']:
            if must_have not in selected:
                selected.insert(0, must_have)
        return selected[:20]

    def fetch_bybit_tickers(self) -> List[Dict]:
        """Fetch all tickers from Bybit for symbol selection"""
        try:
            resp = self.bybit.get_tickers(category='linear')
            items = resp.get('result', {}).get('list', [])
            return [{'symbol': t.get('symbol', ''), 'turnover24h': float(t.get('turnover24h', 0)),
                     'price24hPcnt': float(t.get('price24hPcnt', 0))} for t in items]
        except Exception as e:
            self.log('error', f"Failed to fetch Bybit tickers: {e}")
            return []
    
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
    
    def _extract_symbols(self, title: str, summary: str = '') -> List[str]:
        """Extract trading symbols from news title/summary.
        
        Maps mentions to tradable pairs: BTC, ETH, SOL, XRP, ADA, etc.
        Simple case-insensitive substring matching.
        """
        text = (title + ' ' + summary).upper()
        
        # Symbol → list of aliases to match
        symbol_map = {
            'BTC': ['BITCOIN', 'BTC', 'BTCUSDT'],
            'ETH': ['ETHEREUM', 'ETHER', 'ETH', 'ETHUSDT'],
            'SOL': ['SOLANA', 'SOLUSDT', 'SOL'],
            'XRP': ['XRP', 'XRPUSDT'],
            'ADA': ['ADA', 'ADAUSDT'],
            'DOGE': ['DOGECOIN', 'DOGEUSDT', 'DOGE'],
            'DOT': ['POLKADOT', 'DOTUSDT'],
            'AVAX': ['AVALANCHE', 'AVAXUSDT'],
            'LINK': ['CHAINLINK', 'LINKUSDT'],
            'MATIC': ['POLYGON', 'MATICUSDT'],
            'UNI': ['UNISWAP', 'UNIUSDT'],
            'ATOM': ['COSMOS', 'ATOMUSDT'],
            'LTC': ['LITECOIN', 'LTCUSDT'],
            'BCH': ['BITCOIN CASH', 'BCH'],
            'BNB': ['BNB', 'BINANCE COIN'],
        }
        
        found = []
        for symbol, aliases in symbol_map.items():
            for alias in aliases:
                if alias.upper() in text:
                    if symbol not in found:
                        found.append(symbol)
                    break
        
        return found[:5]

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
                    title = entry.get('title', '')
                    summary = entry.get('summary', '')[:500]
                    symbols = self._extract_symbols(title, summary)
                    
                    news_item = {
                        'source': feed['name'],
                        'title': title,
                        'url': entry.get('link', ''),
                        'published_at': datetime.utcnow(),
                        'summary': summary,
                        'language': feed.get('language', 'en'),
                        'symbols': symbols,
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
        
        # 2. Collect OHLCV for each symbol — Bybit source (all timeframes)
        timeframes_bybit = {'1m': '1', '5m': '5', '15m': '15', '1h': '60', '4h': '240'}

        for symbol in symbols[:8]:
            for tf, bybit_interval in timeframes_bybit.items():
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
