import time
import hmac
import hashlib
import requests
import json
import logging
from typing import Dict, Any, Optional, List
from urllib.parse import urlencode

class CoinExAPIError(Exception):
    """CoinEx API error"""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"CoinEx API Error {code}: {message}")


class CoinExAPI:
    """
    CoinEx V2 REST API Wrapper with rate limiting and signing.
    Docs: https://docs.coinex.com/api/v2
    """
    
    MIN_INTERVAL = 0.050  # 50ms between requests
    last_call_time = 0
    
    def __init__(self, access_id: str, secret_key: str, testnet: bool = False,
                 logger: Optional[logging.Logger] = None):
        self.access_id = access_id
        self.secret_key = secret_key
        self.testnet = testnet
        self.base_url = "https://api.coinex.com"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'CryptoTrader/1.0',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })
        self.logger = logger or logging.getLogger(__name__)
    
    def _ensure_rate_limit(self):
        now = time.time()
        elapsed = now - self.last_call_time
        if elapsed < self.MIN_INTERVAL:
            time.sleep(self.MIN_INTERVAL - elapsed)
        self.last_call_time = time.time()
    
    def _sign(self, method: str, path: str, params: Optional[Dict] = None,
              body: Optional[str] = None) -> Dict[str, str]:
        """
        Generate HMAC-SHA256 signature for CoinEx V2 API.
        Format: HTTP_METHOD + /v2/PATH + timestamp + body
        """
        timestamp = str(int(time.time() * 1000))
        
        # Build the string to sign
        if method.upper() == 'GET' and params:
            query_str = urlencode(sorted(params.items()))
            path = f"{path}?{query_str}"
        
        prepared_body = body or ''
        string_to_sign = f"{method.upper()}{path}{timestamp}{prepared_body}"
        
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return {
            'X-COINEX-KEY': self.access_id,
            'X-COINEX-SIGN': signature,
            'X-COINEX-TIMESTAMP': timestamp,
        }
    
    def _make_request(self, method: str, path: str,
                      params: Optional[Dict] = None,
                      body: Optional[Dict] = None,
                      auth: bool = False) -> Dict:
        self._ensure_rate_limit()
        
        url = f"{self.base_url}{path}"
        headers = {}
        
        if auth:
            body_str = json.dumps(body) if body else ''
            sign_headers = self._sign(method, path, params, body_str)
            headers.update(sign_headers)
        
        try:
            if method.upper() == 'GET':
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
            else:
                resp = self.session.post(url, json=body, headers=headers, timeout=30)
            
            data = resp.json()
            
            # CoinEx V2 returns: {"code": 0, "data": {...}, "message": "OK"}
            if data.get('code', 0) != 0:
                raise CoinExAPIError(data.get('code', -1), data.get('message', 'Unknown error'))
            
            return data.get('data', data)
        except requests.RequestException as e:
            raise CoinExAPIError(-1, f"Request failed: {str(e)}")
    
    # ==================== Market Data (Public) ====================
    
    def get_market_info(self, market: str = 'BTCUSDT') -> Dict:
        """Get market information"""
        return self._make_request('GET', '/v2/spot/market', params={'market': market})
    
    def get_tickers(self, market_type: str = 'SPOT') -> List[Dict]:
        """Get all tickers"""
        return self._make_request('GET', '/v2/spot/ticker', params={'market_type': market_type})
    
    def get_ticker(self, market: str) -> Dict:
        """Get ticker for a specific market"""
        return self._make_request('GET', '/v2/spot/ticker', params={'market': market})
    
    def get_klines(self, market: str, period: int = 60, limit: int = 100) -> List:
        """
        Get K-line (candlestick) data.
        Period in minutes: 1, 3, 5, 15, 30, 60, 120, 240, 360, 720, 1440, 4320, 10080
        """
        return self._make_request('GET', '/v2/spot/kline', params={
            'market': market,
            'period': period,
            'limit': limit,
        })
    
    def get_depth(self, market: str, limit: int = 20, interval: str = '0') -> Dict:
        """Get order book depth"""
        return self._make_request('GET', '/v2/spot/depth', params={
            'market': market,
            'limit': limit,
            'interval': interval,
        })
    
    def get_trades(self, market: str, limit: int = 100) -> List[Dict]:
        """Get recent trades"""
        return self._make_request('GET', '/v2/spot/trade', params={
            'market': market,
            'limit': limit,
        })
    
    # ==================== Account (Authenticated) ====================
    
    def get_balance(self) -> Dict:
        """Get spot account balance"""
        return self._make_request('GET', '/v2/spot/balance', auth=True)
    
    def get_balances_list(self) -> List[Dict]:
        """Get non-zero balances"""
        data = self.get_balance()
        balances = data if isinstance(data, dict) else {}
        return [
            {'asset': k, 'available': float(v.get('available', 0)), 'frozen': float(v.get('frozen', 0))}
            for k, v in balances.items()
            if float(v.get('available', 0)) > 0 or float(v.get('frozen', 0)) > 0
        ]
    
    # ==================== Trading (Authenticated) ====================
    
    def create_order(self, market: str, side: str, order_type: str = 'market',
                     amount: str = '', price: str = '', client_id: str = '') -> Dict:
        """
        Create a spot order.
        
        Args:
            market: Trading pair (e.g., 'BTCUSDT')
            side: 'buy' or 'sell'
            order_type: 'limit' or 'market'
            amount: For BUY: quote amount (USDT). For SELL: base amount (BTC)
            price: Limit price (required for limit orders)
            client_id: Client-defined order ID
        """
        body = {
            'market': market,
            'side': side,
            'type': order_type,
            'amount': amount,
        }
        if price:
            body['price'] = price
        if client_id:
            body['client_id'] = client_id
        
        return self._make_request('POST', '/v2/spot/order', body=body, auth=True)
    
    def create_market_buy(self, market: str, amount_usdt: str) -> Dict:
        """Create market buy order (amount in USDT)"""
        return self.create_order(market=market, side='buy', order_type='market', amount=amount_usdt)
    
    def create_market_sell(self, market: str, amount: str) -> Dict:
        """Create market sell order (amount in base currency)"""
        return self.create_order(market=market, side='sell', order_type='market', amount=amount)
    
    def create_limit_buy(self, market: str, amount: str, price: str) -> Dict:
        """Create limit buy order"""
        return self.create_order(market=market, side='buy', order_type='limit',
                                 amount=amount, price=price)
    
    def create_limit_sell(self, market: str, amount: str, price: str) -> Dict:
        """Create limit sell order"""
        return self.create_order(market=market, side='sell', order_type='limit',
                                 amount=amount, price=price)
    
    def cancel_order(self, market: str, order_id: int) -> Dict:
        """Cancel an order"""
        body = {'market': market, 'order_id': order_id}
        return self._make_request('POST', '/v2/spot/cancel-order', body=body, auth=True)
    
    def cancel_all_orders(self, market: str) -> Dict:
        """Cancel all orders for a market"""
        body = {'market': market}
        return self._make_request('POST', '/v2/spot/cancel-all', body=body, auth=True)
    
    def get_pending_orders(self, market: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """Get pending (open) orders"""
        params = {'limit': limit}
        if market:
            params['market'] = market
        return self._make_request('GET', '/v2/spot/pending-order', params=params, auth=True)
    
    def get_finished_orders(self, market: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """Get finished (filled/cancelled) orders"""
        params = {'limit': limit}
        if market:
            params['market'] = market
        return self._make_request('GET', '/v2/spot/finished-order', params=params, auth=True)
    
    def get_order_detail(self, market: str, order_id: int) -> Dict:
        """Get order details"""
        return self._make_request('GET', '/v2/spot/order-detail', 
                                  params={'market': market, 'order_id': order_id}, auth=True)
    
    def get_user_trades(self, market: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """Get user trade history"""
        params = {'limit': limit}
        if market:
            params['market'] = market
        return self._make_request('GET', '/v2/spot/user-trade', params=params, auth=True)
    
    # ==================== Futures (Optional) ====================
    
    def get_futures_balance(self) -> Dict:
        """Get futures account balance"""
        return self._make_request('GET', '/v2/futures/balance', auth=True)
    
    # ==================== Utilities ====================
    
    @staticmethod
    def to_coinex_symbol(symbol: str) -> str:
        """Convert Binance format (BTCUSDT) to CoinEx format (BTCUSDT)"""
        # CoinEx uses same format as Binance: BTCUSDT, ETHUSDT
        return symbol
    
    @staticmethod
    def from_coinex_symbol(symbol: str) -> str:
        """CoinEx symbol to standard format"""
        return symbol
    
    def check_connection(self) -> bool:
        """Test API connection"""
        try:
            self.get_ticker('BTCUSDT')
            return True
        except Exception as e:
            self.logger.error(f"CoinEx connection failed: {e}")
            return False
    
    def timeframe_to_period(self, timeframe: str) -> int:
        """Convert timeframe string to CoinEx period (minutes)"""
        mapping = {
            '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
            '1h': 60, '2h': 120, '4h': 240, '6h': 360,
            '12h': 720, '1d': 1440, '3d': 4320, '1w': 10080,
        }
        return mapping.get(timeframe, 60)
