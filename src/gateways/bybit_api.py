import time
import hmac
import hashlib
import requests
import json
import logging
from typing import Dict, Any, Optional, List

class BybitAPIError(Exception):
    """Bybit API error"""
    def __init__(self, ret_code: int, ret_msg: str, **kwargs):
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.data = kwargs
        super().__init__(f"Bybit API Error {ret_code}: {ret_msg}")

class BybitAPI:
    """Bybit V5 REST API Wrapper with rate limiting and signing"""
    
    MIN_INTERVAL_GET = 0.100    # 100ms for GET
    MIN_INTERVAL_POST = 0.300   # 300ms for POST
    last_call_time = 0
    
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, 
                 recv_window: int = 5000, logger: Optional[logging.Logger] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.recv_window = recv_window
        self.base_url = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'bybit-skill/1.2.3',
            'X-Referer': 'bybit-skill',
        })
        self.logger = logger or logging.getLogger(__name__)
    
    def _ensure_rate_limit(self, is_post: bool = False):
        now = time.time()
        min_interval = self.MIN_INTERVAL_POST if is_post else self.MIN_INTERVAL_GET
        elapsed = now - self.last_call_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self.last_call_time = time.time()
    
    def _sign(self, timestamp: str, param_str: str) -> str:
        sign_str = f"{timestamp}{self.api_key}{self.recv_window}{param_str}"
        return hmac.new(
            self.api_secret.encode('utf-8'),
            sign_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    def _make_request(self, method: str, endpoint: str, 
                      params: Optional[Dict] = None,
                      json_data: Optional[Dict] = None,
                      auth: bool = True) -> Dict:
        self._ensure_rate_limit(is_post=(method.upper() == 'POST'))
        
        url = f"{self.base_url}{endpoint}"
        headers = {}
        
        if auth:
            timestamp = str(int(time.time() * 1000))
            
            if method.upper() == 'GET' and params:
                param_str = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
            elif method.upper() == 'POST' and json_data:
                param_str = json.dumps(json_data, separators=(',', ':'))
            else:
                param_str = ''
            
            signature = self._sign(timestamp, param_str)
            headers.update({
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-SIGN': signature,
                'X-BAPI-RECV-WINDOW': str(self.recv_window),
            })
            if method.upper() == 'POST':
                headers['Content-Type'] = 'application/json'
        
        try:
            if method.upper() == 'GET':
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
            else:
                body = json.dumps(json_data, separators=(',', ':')) if json_data else None
                resp = self.session.post(url, data=body, headers=headers, timeout=30)
            
            data = resp.json()
            
            if data.get('retCode', 0) != 0:
                raise BybitAPIError(data.get('retCode', -1), data.get('retMsg', 'Unknown error'))
            
            return data
        except requests.RequestException as e:
            raise BybitAPIError(-1, f"Request failed: {str(e)}")
    
    # ==================== Market Data ====================
    
    def get_tickers(self, category: str = 'spot', symbol: Optional[str] = None) -> Dict:
        """Get ticker information"""
        params = {'category': category}
        if symbol:
            params['symbol'] = symbol
        return self._make_request('GET', '/v5/market/tickers', params=params, auth=False)
    
    def get_ticker(self, symbol: str) -> Dict:
        """Get ticker for a single symbol"""
        data = self.get_tickers('spot', symbol)
        if data.get('result', {}).get('list'):
            return data['result']['list'][0]
        return {}
    
    def get_kline(self, symbol: str, interval: str = '60', category: str = 'linear',
                  limit: int = 200) -> Dict:
        """Get kline data"""
        params = {
            'symbol': symbol,
            'interval': interval,
            'category': category,
            'limit': limit
        }
        return self._make_request('GET', '/v5/market/kline', params=params, auth=False)
    
    def get_orderbook(self, symbol: str, category: str = 'spot', limit: int = 25) -> Dict:
        """Get orderbook depth"""
        params = {'symbol': symbol, 'category': category, 'limit': limit}
        return self._make_request('GET', '/v5/market/orderbook', params=params, auth=False)
    
    def get_instruments_info(self, category: str = 'spot', symbol: Optional[str] = None) -> Dict:
        """Get instrument info"""
        params = {'category': category}
        if symbol:
            params['symbol'] = symbol
        return self._make_request('GET', '/v5/market/instruments-info', params=params, auth=False)
    
    # ==================== Account ====================
    
    def get_wallet_balance(self, account_type: str = 'UNIFIED') -> Dict:
        """Get wallet balance"""
        return self._make_request('GET', '/v5/account/wallet-balance',
                                  params={'accountType': account_type})
    
    def get_account_info(self) -> Dict:
        """Get account info"""
        return self._make_request('GET', '/v5/account/info')
    
    # ==================== Trading ====================
    
    def create_order(self, category: str, symbol: str, side: str, order_type: str,
                     qty: str, price: Optional[str] = None, market_unit: str = 'baseCoin',
                     time_in_force: str = 'GTC', position_idx: int = 0,
                     stop_loss: Optional[str] = None, take_profit: Optional[str] = None,
                     reduce_only: bool = False, tpsl_mode: str = 'Full',
                     order_link_id: Optional[str] = None) -> Dict:
        """Create order"""
        json_data = {
            'category': category,
            'symbol': symbol,
            'side': side,
            'orderType': order_type,
            'qty': qty,
            'timeInForce': time_in_force,
        }
        
        if category == 'spot' and side == 'Buy':
            json_data['marketUnit'] = market_unit
        
        if price:
            json_data['price'] = price
        if category == 'linear':
            json_data['positionIdx'] = position_idx
            json_data['reduceOnly'] = reduce_only
            json_data['tpslMode'] = tpsl_mode
        if stop_loss:
            json_data['stopLoss'] = stop_loss
        if take_profit:
            json_data['takeProfit'] = take_profit
        if order_link_id:
            json_data['orderLinkId'] = order_link_id
        
        return self._make_request('POST', '/v5/order/create', json_data=json_data)
    
    def create_spot_buy(self, symbol: str, qty: str, order_type: str = 'Market',
                         price: Optional[str] = None) -> Dict:
        """Create spot buy order"""
        return self.create_order(
            category='spot', symbol=symbol, side='Buy',
            order_type=order_type, qty=qty, price=price,
            market_unit='quoteCoin'
        )
    
    def create_spot_sell(self, symbol: str, qty: str, order_type: str = 'Market',
                          price: Optional[str] = None) -> Dict:
        """Create spot sell order"""
        return self.create_order(
            category='spot', symbol=symbol, side='Sell',
            order_type=order_type, qty=qty, price=price,
            market_unit='baseCoin'
        )
    
    def create_linear_long(self, symbol: str, qty: str, leverage: str = '1',
                            order_type: str = 'Market', price: Optional[str] = None,
                            stop_loss: Optional[str] = None, take_profit: Optional[str] = None) -> Dict:
        """Create linear perpetual long position"""
        self.set_leverage(symbol, leverage, leverage)
        return self.create_order(
            category='linear', symbol=symbol, side='Buy',
            order_type=order_type, qty=qty, price=price,
            position_idx=1, stop_loss=stop_loss, take_profit=take_profit
        )
    
    def create_linear_short(self, symbol: str, qty: str, leverage: str = '1',
                             order_type: str = 'Market', price: Optional[str] = None,
                             stop_loss: Optional[str] = None, take_profit: Optional[str] = None) -> Dict:
        """Create linear perpetual short position"""
        self.set_leverage(symbol, leverage, leverage)
        return self.create_order(
            category='linear', symbol=symbol, side='Sell',
            order_type=order_type, qty=qty, price=price,
            position_idx=2, stop_loss=stop_loss, take_profit=take_profit
        )
    
    def cancel_order(self, category: str, symbol: str, 
                     order_id: Optional[str] = None, order_link_id: Optional[str] = None) -> Dict:
        """Cancel order"""
        json_data = {'category': category, 'symbol': symbol}
        if order_id:
            json_data['orderId'] = order_id
        if order_link_id:
            json_data['orderLinkId'] = order_link_id
        return self._make_request('POST', '/v5/order/cancel', json_data=json_data)
    
    def cancel_all_orders(self, category: str, symbol: Optional[str] = None) -> Dict:
        """Cancel all orders"""
        json_data = {'category': category}
        if symbol:
            json_data['symbol'] = symbol
        return self._make_request('POST', '/v5/order/cancel-all', json_data=json_data)
    
    def get_open_orders(self, category: str = 'spot', symbol: Optional[str] = None) -> Dict:
        """Get open orders"""
        params = {'category': category}
        if symbol:
            params['symbol'] = symbol
        return self._make_request('GET', '/v5/order/realtime', params=params)
    
    def get_order_history(self, category: str = 'spot', symbol: Optional[str] = None,
                          limit: int = 50) -> Dict:
        """Get order history"""
        params = {'category': category, 'limit': limit}
        if symbol:
            params['symbol'] = symbol
        return self._make_request('GET', '/v5/order/history', params=params)
    
    # ==================== Position (Derivatives) ====================
    
    def get_positions(self, category: str = 'linear', symbol: Optional[str] = None,
                      settle_coin: str = 'USDT') -> Dict:
        """Get open positions"""
        params = {'category': category}
        if symbol:
            params['symbol'] = symbol
        else:
            params['settleCoin'] = settle_coin
        return self._make_request('GET', '/v5/position/list', params=params)
    
    def set_leverage(self, symbol: str, buy_leverage: str, sell_leverage: str) -> Dict:
        """Set leverage for a symbol"""
        json_data = {
            'category': 'linear',
            'symbol': symbol,
            'buyLeverage': buy_leverage,
            'sellLeverage': sell_leverage,
        }
        return self._make_request('POST', '/v5/position/set-leverage', json_data=json_data)
    
    def set_tp_sl_mode(self, symbol: str, tpsl_mode: str = 'Full') -> Dict:
        """Set TP/SL mode"""
        json_data = {
            'category': 'linear',
            'symbol': symbol,
            'tpslMode': tpsl_mode,
        }
        return self._make_request('POST', '/v5/position/set-tpsl-mode', json_data=json_data)
    
    # ==================== Utilities ====================
    
    def check_clock_sync(self) -> bool:
        """Check if local clock is synced with server"""
        try:
            resp = self._make_request('GET', '/v5/market/time', auth=False)
            server_ts = int(resp['result']['timeSecond'])
            local_ts = int(time.time())
            diff = abs(server_ts - local_ts)
            if diff > 5:
                self.logger.warning(f"Bybit clock skew detected: {diff}s")
                return False
            return True
        except Exception as e:
            self.logger.error(f"Bybit clock sync failed: {e}")
            return False
