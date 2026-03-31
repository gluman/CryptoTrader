import time
import hmac
import hashlib
import requests
import logging
from typing import Dict, Any, Optional, List
from urllib.parse import urlencode

class BinanceAPIError(Exception):
    """Binance API error"""
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"Binance API Error {code}: {msg}")

class BinanceAPI:
    """Binance Spot REST API Wrapper with rate limiting and signing"""
    
    MIN_INTERVAL_GET = 0.050    # 50ms for GET
    MIN_INTERVAL_POST = 0.200   # 200ms for POST
    last_call_time = 0
    
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, logger: Optional[logging.Logger] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.base_url = "https://testnet.binance.vision" if testnet else "https://api.binance.com"
        self.session = requests.Session()
        self.logger = logger or logging.getLogger(__name__)
    
    def _ensure_rate_limit(self, is_post: bool = False):
        now = time.time()
        min_interval = self.MIN_INTERVAL_POST if is_post else self.MIN_INTERVAL_GET
        elapsed = now - self.last_call_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self.last_call_time = time.time()
    
    def _sign(self, params: Dict) -> str:
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _get_headers(self) -> Dict:
        return {
            'X-MBX-APIKEY': self.api_key,
            'User-Agent': 'CryptoTrader/1.0'
        }
    
    def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None, 
                      signed: bool = False) -> Dict:
        self._ensure_rate_limit(is_post=(method.upper() == 'POST'))
        
        if params is None:
            params = {}
        
        url = f"{self.base_url}{endpoint}"
        
        if signed:
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = self._sign(params)
        
        try:
            if method.upper() == 'GET':
                resp = self.session.get(url, params=params, headers=self._get_headers(), timeout=30)
            else:
                resp = self.session.post(url, data=params, headers=self._get_headers(), timeout=30)
            
            data = resp.json()
            
            if 'code' in data and data['code'] != 200:
                raise BinanceAPIError(data['code'], data.get('msg', 'Unknown error'))
            
            return data
        except requests.RequestException as e:
            raise BinanceAPIError(-1, f"Request failed: {str(e)}")
    
    # ==================== Market Data ====================
    
    def get_ticker(self, symbol: str) -> Dict:
        """Get 24hr ticker for a symbol"""
        return self._make_request('GET', '/api/v3/ticker/24hr', {'symbol': symbol})
    
    def get_all_tickers(self) -> List[Dict]:
        """Get all ticker data"""
        return self._make_request('GET', '/api/v3/ticker/24hr')
    
    def get_book_ticker(self, symbol: str) -> Dict:
        """Get best bid/ask"""
        return self._make_request('GET', '/api/v3/ticker/bookTicker', {'symbol': symbol})
    
    def get_klines(self, symbol: str, interval: str, limit: int = 500,
                   start_time: Optional[int] = None, end_time: Optional[int] = None) -> List:
        """Get kline/candlestick data"""
        params = {'symbol': symbol, 'interval': interval, 'limit': limit}
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        return self._make_request('GET', '/api/v3/klines', params)
    
    def get_depth(self, symbol: str, limit: int = 100) -> Dict:
        """Get order book depth"""
        return self._make_request('GET', '/api/v3/depth', {'symbol': symbol, 'limit': limit})
    
    def get_exchange_info(self) -> Dict:
        """Get exchange info (all symbols, filters, etc.)"""
        return self._make_request('GET', '/api/v3/exchangeInfo')
    
    def get_symbol_info(self, symbol: str) -> Optional[Dict]:
        """Get info for a specific symbol"""
        info = self.get_exchange_info()
        for s in info.get('symbols', []):
            if s['symbol'] == symbol:
                return s
        return None
    
    # ==================== Account ====================
    
    def get_account(self) -> Dict:
        """Get account information"""
        return self._make_request('GET', '/api/v3/account', signed=True)
    
    def get_balances(self) -> List[Dict]:
        """Get account balances (non-zero only)"""
        account = self.get_account()
        return [b for b in account.get('balances', []) if float(b['free']) > 0 or float(b['locked']) > 0]
    
    # ==================== Trading ====================
    
    def create_order(self, symbol: str, side: str, order_type: str, 
                     quantity: Optional[str] = None, price: Optional[str] = None,
                     quote_order_qty: Optional[str] = None, time_in_force: str = 'GTC',
                     new_order_resp_type: str = 'RESULT') -> Dict:
        """Create a new order"""
        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'newOrderRespType': new_order_resp_type,
        }
        
        if quantity:
            params['quantity'] = quantity
        if price:
            params['price'] = price
        if order_type == 'LIMIT':
            params['timeInForce'] = time_in_force
        if quote_order_qty:  # For MARKET buy using quote asset quantity
            params['quoteOrderQty'] = quote_order_qty
        
        return self._make_request('POST', '/api/v3/order', params, signed=True)
    
    def create_market_buy(self, symbol: str, quote_order_qty: str) -> Dict:
        """Create market buy order (buy X USDT worth)"""
        return self.create_order(
            symbol=symbol,
            side='BUY',
            order_type='MARKET',
            quote_order_qty=quote_order_qty
        )
    
    def create_market_sell(self, symbol: str, quantity: str) -> Dict:
        """Create market sell order (sell X quantity of base asset)"""
        return self.create_order(
            symbol=symbol,
            side='SELL',
            order_type='MARKET',
            quantity=quantity
        )
    
    def create_limit_buy(self, symbol: str, quantity: str, price: str, 
                         time_in_force: str = 'GTC') -> Dict:
        """Create limit buy order"""
        return self.create_order(
            symbol=symbol,
            side='BUY',
            order_type='LIMIT',
            quantity=quantity,
            price=price,
            time_in_force=time_in_force
        )
    
    def create_limit_sell(self, symbol: str, quantity: str, price: str,
                          time_in_force: str = 'GTC') -> Dict:
        """Create limit sell order"""
        return self.create_order(
            symbol=symbol,
            side='SELL',
            order_type='LIMIT',
            quantity=quantity,
            price=price,
            time_in_force=time_in_force
        )
    
    def cancel_order(self, symbol: str, order_id: int) -> Dict:
        """Cancel an open order"""
        return self._make_request('DELETE', '/api/v3/order', 
                                  {'symbol': symbol, 'orderId': order_id}, signed=True)
    
    def get_order(self, symbol: str, order_id: int) -> Dict:
        """Query order status"""
        return self._make_request('GET', '/api/v3/order',
                                  {'symbol': symbol, 'orderId': order_id}, signed=True)
    
    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get all open orders"""
        params = {}
        if symbol:
            params['symbol'] = symbol
        return self._make_request('GET', '/api/v3/openOrders', params, signed=True)
    
    def cancel_all_orders(self, symbol: str) -> List[Dict]:
        """Cancel all open orders for a symbol"""
        return self._make_request('DELETE', '/api/v3/openOrders', 
                                  {'symbol': symbol}, signed=True)
    
    # ==================== My Trades ====================
    
    def get_my_trades(self, symbol: str, limit: int = 500) -> List[Dict]:
        """Get trade history"""
        return self._make_request('GET', '/api/v3/myTrades',
                                  {'symbol': symbol, 'limit': limit}, signed=True)
    
    # ==================== Utilities ====================
    
    def check_clock_sync(self) -> bool:
        """Check if local clock is synced with server"""
        try:
            server_time = self._make_request('GET', '/api/v3/time')
            server_ts = server_time['serverTime'] / 1000.0
            local_ts = time.time()
            diff = abs(server_ts - local_ts)
            if diff > 5:
                self.logger.warning(f"Clock skew detected: {diff:.1f}s")
                return False
            return True
        except Exception as e:
            self.logger.error(f"Clock sync check failed: {e}")
            return False
    
    def format_quantity(self, symbol: str, quantity: float) -> str:
        """Format quantity according to symbol filters"""
        info = self.get_symbol_info(symbol)
        if not info:
            return str(quantity)
        
        for f in info.get('filters', []):
            if f['filterType'] == 'LOT_SIZE':
                step_size = float(f['stepSize'])
                # Round to step size
                precision = len(str(step_size).rstrip('0').split('.')[-1])
                return f"{quantity:.{precision}f}"
        return str(quantity)
    
    def format_price(self, symbol: str, price: float) -> str:
        """Format price according to symbol filters"""
        info = self.get_symbol_info(symbol)
        if not info:
            return str(price)
        
        for f in info.get('filters', []):
            if f['filterType'] == 'PRICE_FILTER':
                tick_size = float(f['tickSize'])
                precision = len(str(tick_size).rstrip('0').split('.')[-1])
                return f"{price:.{precision}f}"
        return str(price)
