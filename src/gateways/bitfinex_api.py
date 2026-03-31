import time
import hmac
import hashlib
import base64
import requests
import json
import logging
from typing import Dict, Any, Optional, List

class BitfinexAPIError(Exception):
    """Bitfinex API error"""
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"Bitfinex API Error {code}: {msg}")

class BitfinexAPI:
    """Bitfinex REST API Wrapper (v2 and v1 endpoints)"""
    
    MIN_INTERVAL = 0.100  # 100ms between requests
    last_call_time = 0
    
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, 
                 logger: Optional[logging.Logger] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        # Bitfinex has separate public and auth endpoints
        self.base_url = "https://api-pub.bitfinex.com"     # Public v2
        self.auth_url = "https://api.bitfinex.com"          # Authenticated
        self.session = requests.Session()
        self.logger = logger or logging.getLogger(__name__)
    
    def _ensure_rate_limit(self):
        now = time.time()
        elapsed = now - self.last_call_time
        if elapsed < self.MIN_INTERVAL:
            time.sleep(self.MIN_INTERVAL - elapsed)
        self.last_call_time = time.time()
    
    def _sign(self, endpoint: str, body: str = '') -> Dict[str, str]:
        """Generate HMAC-SHA384 signature for authenticated endpoints"""
        nonce = str(int(time.time() * 1000))
        message = '/api/v2' + endpoint + nonce + body
        
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha384
        ).hexdigest()
        
        return {
            'bfx-nonce': nonce,
            'bfx-apikey': self.api_key,
            'bfx-signature': signature,
            'Content-Type': 'application/json',
        }
    
    def _make_public_request(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        """Make a public (unauthenticated) request"""
        self._ensure_rate_limit()
        
        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise BitfinexAPIError(-1, f"Request failed: {str(e)}")
    
    def _make_auth_request(self, endpoint: str, body: Optional[Dict] = None) -> Any:
        """Make an authenticated POST request"""
        self._ensure_rate_limit()
        
        url = f"{self.auth_url}/v2{endpoint}"
        body_str = json.dumps(body) if body else ''
        headers = self._sign(endpoint, body_str)
        
        try:
            resp = self.session.post(url, headers=headers, data=body_str, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            # Bitfinex returns error as array: [151000, "error", "ERROR_CODE", "message"]
            if isinstance(data, list) and len(data) >= 4 and data[0] == 151000:
                raise BitfinexAPIError(data[2], data[3])
            
            return data
        except requests.RequestException as e:
            raise BitfinexAPIError(-1, f"Request failed: {str(e)}")
    
    # ==================== Market Data (Public) ====================
    
    def get_tickers(self, symbols: List[str]) -> List[List]:
        """Get tickers for multiple symbols. Format: tBTCUSD, tETHUSD"""
        symbol_str = ','.join(symbols)
        return self._make_public_request(f"/v2/tickers?symbols={symbol_str}")
    
    def get_ticker(self, symbol: str) -> List:
        """Get ticker for a single symbol"""
        return self._make_public_request(f"/v2/ticker/{symbol}")
    
    def get_candles(self, symbol: str, timeframe: str = '1h', 
                    section: str = 'hist', limit: int = 100) -> List[List]:
        """
        Get candlestick data. Bitfinex uses tBTCUSD format.
        Timeframe: 1m, 5m, 15m, 30m, 1h, 3h, 6h, 12h, 1D, 1W, 14D, 1M
        """
        return self._make_public_request(
            f"/v2/candles/trade:{timeframe}:{symbol}/{section}",
            params={'limit': limit, 'sort': -1}
        )
    
    def get_trades(self, symbol: str, limit: int = 50) -> List[List]:
        """Get recent trades"""
        return self._make_public_request(
            f"/v2/trades/{symbol}/hist",
            params={'limit': limit, 'sort': -1}
        )
    
    def get_orderbook(self, symbol: str, precision: str = 'P0', limit: int = 25) -> List[List]:
        """Get orderbook"""
        return self._make_public_request(
            f"/v2/book/{symbol}/{precision}",
            params={'len': limit}
        )
    
    # ==================== Account (Authenticated) ====================
    
    def get_wallets(self) -> List[List]:
        """Get wallet balances"""
        return self._make_auth_request('/auth/r/wallets')
    
    def get_balances(self) -> List[List]:
        """Get account balances (simplified)"""
        wallets = self.get_wallets()
        # Format: [wallet_type, currency, balance, unsettled_interest, balance_available]
        return [w for w in wallets if float(w[2]) > 0]
    
    # ==================== Trading (Authenticated) ====================
    
    def create_order(self, symbol: str, amount: str, price: str, side: str,
                     order_type: str = 'EXCHANGE MARKET', flags: int = 0) -> Dict:
        """
        Create an order.
        
        Order types:
        - EXCHANGE MARKET: Market order on exchange
        - EXCHANGE LIMIT: Limit order on exchange
        - MARKET: Margin market order
        - LIMIT: Margin limit order
        
        Flags:
        - 0: None
        - 4096: Post-only (maker only)
        """
        payload = {
            'type': order_type,
            'symbol': symbol,
            'amount': amount,
            'price': price,
        }
        return self._make_auth_request('/auth/w/order/submit', payload)
    
    def create_market_buy(self, symbol: str, amount_usd: str) -> List:
        """Create market buy order (amount in USD)"""
        return self.create_order(
            symbol=symbol,
            amount=amount_usd,  # Positive for buy
            price='0',          # Market order uses 0
            side='buy',
            order_type='EXCHANGE MARKET'
        )
    
    def create_market_sell(self, symbol: str, amount: str) -> List:
        """Create market sell order (amount in base currency, negative)"""
        return self.create_order(
            symbol=symbol,
            amount=amount,      # Negative for sell
            price='0',
            side='sell',
            order_type='EXCHANGE MARKET'
        )
    
    def create_limit_buy(self, symbol: str, amount: str, price: str) -> List:
        """Create limit buy order"""
        return self.create_order(
            symbol=symbol,
            amount=amount,
            price=price,
            side='buy',
            order_type='EXCHANGE LIMIT'
        )
    
    def create_limit_sell(self, symbol: str, amount: str, price: str) -> List:
        """Create limit sell order"""
        return self.create_order(
            symbol=symbol,
            amount=amount,
            price=price,
            side='sell',
            order_type='EXCHANGE LIMIT'
        )
    
    def cancel_order(self, order_id: int) -> List:
        """Cancel an order by ID"""
        return self._make_auth_request('/auth/w/order/cancel', {'id': order_id})
    
    def get_open_orders(self) -> List[List]:
        """Get all open orders"""
        return self._make_auth_request('/auth/r/orders')
    
    def get_order_history(self, symbol: Optional[str] = None, limit: int = 50) -> List[List]:
        """Get order history"""
        endpoint = '/auth/r/orders/hist'
        if symbol:
            endpoint += f'/{symbol}'
        return self._make_auth_request(endpoint)
    
    def get_trades_history(self, symbol: Optional[str] = None, limit: int = 50) -> List[List]:
        """Get trade history"""
        endpoint = '/auth/r/trades/hist'
        if symbol:
            endpoint += f'/{symbol}'
        return self._make_auth_request(endpoint)
    
    # ==================== Utilities ====================
    
    @staticmethod
    def to_bitfinex_symbol(symbol: str) -> str:
        """Convert Binance-style symbol (BTCUSDT) to Bitfinex format (tBTCUST)"""
        # Remove trailing quote currencies
        for quote in ['UST', 'USDT', 'USD', 'BTC', 'ETH', 'EUR']:
            if symbol.endswith(quote):
                base = symbol[:-len(quote)]
                if quote == 'USDT':
                    quote = 'UST'
                return f"t{base}{quote}"
        return f"t{symbol}"
    
    @staticmethod
    def from_bitfinex_symbol(symbol: str) -> str:
        """Convert Bitfinex symbol (tBTCUST) to Binance format (BTCUSDT)"""
        if symbol.startswith('t'):
            symbol = symbol[1:]
        if symbol.endswith('UST'):
            symbol = symbol[:-3] + 'USDT'
        return symbol
    
    def check_connection(self) -> bool:
        """Test API connection"""
        try:
            self.get_ticker('tBTCUSD')
            return True
        except Exception as e:
            self.logger.error(f"Bitfinex connection failed: {e}")
            return False
