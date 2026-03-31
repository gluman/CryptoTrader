import logging
from datetime import datetime
from typing import Dict, Any, Optional
from .base import BaseAgent
from ..core.config import Config
from ..core.database import DatabaseManager, Signal, Trade
from ..gateways import BinanceAPI, BybitAPI, BitfinexAPI

class ExecutionAgent(BaseAgent):
    """Executes trading decisions with safety checks"""
    
    def __init__(self, config: Config, logger: logging.Logger, db: DatabaseManager):
        super().__init__('Execution', logger)
        self.config = config
        self.db = db
        self.exchanges = {}
        self._init_exchanges()
        self.testnet_first = config.agents.get('executor', {}).get('testnet_first', True)
        self.confirmation_required = config.agents.get('executor', {}).get('confirmation_required', True)
    
    def _init_exchanges(self):
        """Initialize exchange connections"""
        # Binance
        if self.config.binance:
            self.exchanges['binance'] = BinanceAPI(
                api_key=self.config.binance['api_key'],
                api_secret=self.config.binance['api_secret'],
                testnet=self.config.binance.get('testnet', False),
                logger=self.logger
            )
        
        # Bybit
        if self.config.bybit:
            self.exchanges['bybit'] = BybitAPI(
                api_key=self.config.bybit['api_key'],
                api_secret=self.config.bybit['api_secret'],
                testnet=self.config.bybit.get('testnet', False),
                logger=self.logger
            )
        
        # Bitfinex
        if self.config.bitfinex:
            self.exchanges['bitfinex'] = BitfinexAPI(
                api_key=self.config.bitfinex['api_key'],
                api_secret=self.config.bitfinex['api_secret'],
                testnet=self.config.bitfinex.get('testnet', False),
                logger=self.logger
            )
    
    def get_balance(self, exchange: str = 'binance') -> Dict:
        """Get account balance"""
        try:
            ex = self.exchanges.get(exchange)
            if not ex:
                raise ValueError(f"Unknown exchange: {exchange}")
            
            if exchange == 'binance':
                account = ex.get_account()
                balances = [
                    {'asset': b['asset'], 'free': float(b['free']), 'locked': float(b['locked'])}
                    for b in account.get('balances', []) 
                    if float(b['free']) > 0 or float(b['locked']) > 0
                ]
                return {'exchange': exchange, 'balances': balances}
            
            elif exchange == 'bybit':
                data = ex.get_wallet_balance('UNIFIED')
                balances = data['result']['list'][0]['coin']
                return {'exchange': exchange, 'balances': [
                    {'asset': b['coin'], 'free': float(b['availableToWithdraw']), 
                     'total': float(b['walletBalance'])}
                    for b in balances if float(b['walletBalance']) > 0
                ]}
            
            elif exchange == 'bitfinex':
                wallets = ex.get_balances()
                return {'exchange': exchange, 'balances': [
                    {'asset': w[1], 'free': float(w[4]) if len(w) > 4 else float(w[2]), 
                     'total': float(w[2])}
                    for w in wallets
                ]}
            
        except Exception as e:
            self.log('error', f"Failed to get balance from {exchange}: {e}")
            return {'exchange': exchange, 'balances': [], 'error': str(e)}
    
    def execute_spot_buy(self, symbol: str, quote_amount: str, 
                          exchange: str = 'binance') -> Dict:
        """Execute spot market buy"""
        try:
            ex = self.exchanges[exchange]
            
            if exchange == 'binance':
                result = ex.create_market_buy(symbol, quote_amount)
                order_id = result.get('orderId')
                self.log('info', f"Binance BUY {symbol}: {quote_amount} USDT → orderId={order_id}")
                return {
                    'exchange': exchange,
                    'symbol': symbol,
                    'side': 'BUY',
                    'order_id': str(order_id),
                    'status': result.get('status'),
                    'executed_qty': result.get('executedQty'),
                    'cummulative_quote_qty': result.get('cummulativeQuoteQty'),
                }
            
            elif exchange == 'bybit':
                result = ex.create_spot_buy(symbol, quote_amount)
                order_id = result['result'].get('orderId')
                self.log('info', f"Bybit BUY {symbol}: {quote_amount} USDT → orderId={order_id}")
                return {
                    'exchange': exchange,
                    'symbol': symbol,
                    'side': 'BUY',
                    'order_id': order_id,
                    'status': 'FILLED',
                }
            
            elif exchange == 'bitfinex':
                bfx_symbol = BitfinexAPI.to_bitfinex_symbol(symbol)
                result = ex.create_market_buy(bfx_symbol, quote_amount)
                order_id = result[0][0] if result and result[0] else None
                self.log('info', f"Bitfinex BUY {symbol}: {quote_amount} → orderId={order_id}")
                return {
                    'exchange': exchange,
                    'symbol': symbol,
                    'side': 'BUY',
                    'order_id': str(order_id),
                    'status': 'FILLED',
                }
        
        except Exception as e:
            self.log('error', f"Spot buy failed on {exchange}: {e}")
            return {'exchange': exchange, 'symbol': symbol, 'side': 'BUY', 'error': str(e)}
    
    def execute_spot_sell(self, symbol: str, quantity: str,
                           exchange: str = 'binance') -> Dict:
        """Execute spot market sell"""
        try:
            ex = self.exchanges[exchange]
            
            if exchange == 'binance':
                result = ex.create_market_sell(symbol, quantity)
                order_id = result.get('orderId')
                self.log('info', f"Binance SELL {symbol}: {quantity} → orderId={order_id}")
                return {
                    'exchange': exchange,
                    'symbol': symbol,
                    'side': 'SELL',
                    'order_id': str(order_id),
                    'status': result.get('status'),
                    'executed_qty': result.get('executedQty'),
                }
            
            elif exchange == 'bybit':
                result = ex.create_spot_sell(symbol, quantity)
                order_id = result['result'].get('orderId')
                self.log('info', f"Bybit SELL {symbol}: {quantity} → orderId={order_id}")
                return {
                    'exchange': exchange,
                    'symbol': symbol,
                    'side': 'SELL',
                    'order_id': order_id,
                    'status': 'FILLED',
                }
            
            elif exchange == 'bitfinex':
                bfx_symbol = BitfinexAPI.to_bitfinex_symbol(symbol)
                sell_amount = f"-{quantity}"  # Negative for sell
                result = ex.create_market_sell(bfx_symbol, sell_amount)
                order_id = result[0][0] if result and result[0] else None
                self.log('info', f"Bitfinex SELL {symbol}: {quantity} → orderId={order_id}")
                return {
                    'exchange': exchange,
                    'symbol': symbol,
                    'side': 'SELL',
                    'order_id': str(order_id),
                    'status': 'FILLED',
                }
        
        except Exception as e:
            self.log('error', f"Spot sell failed on {exchange}: {e}")
            return {'exchange': exchange, 'symbol': symbol, 'side': 'SELL', 'error': str(e)}
    
    def save_trade_to_db(self, signal_id: Optional[int], result: Dict):
        """Save executed trade to database"""
        with self.db.get_session() as session:
            trade = Trade(
                signal_id=signal_id,
                exchange=result.get('exchange', 'unknown'),
                symbol=result.get('symbol', ''),
                side=result.get('side', ''),
                order_type='MARKET',
                quantity=float(result.get('executed_qty', 0)),
                order_id=result.get('order_id'),
                created_at=datetime.utcnow(),
            )
            session.add(trade)
            
            # Update signal status
            if signal_id:
                signal = session.query(Signal).filter_by(id=signal_id).first()
                if signal:
                    signal.status = 'EXECUTED'
                    signal.executed_at = datetime.utcnow()
    
    def update_signal_status(self, signal_id: int, status: str, 
                              pnl_percent: Optional[float] = None):
        """Update signal status"""
        with self.db.get_session() as session:
            signal = session.query(Signal).filter_by(id=signal_id).first()
            if signal:
                signal.status = status
                signal.updated_at = datetime.utcnow()
                if pnl_percent is not None:
                    signal.pnl_percent = pnl_percent
    
    def run_once(self) -> Dict[str, Any]:
        """Execute pending signals"""
        self.log('info', "Checking for pending signals...")
        
        # Get pending signals
        with self.db.get_session() as session:
            pending = session.query(Signal).filter_by(status='PENDING').order_by(
                Signal.created_at.desc()
            ).limit(5).all()
        
        results = []
        for signal in pending:
            try:
                if signal.signal_type == 'BUY':
                    # Calculate position size (1% of balance by default)
                    balance = self.get_balance('binance')
                    usdt_balance = 0
                    for b in balance.get('balances', []):
                        if b['asset'] == 'USDT':
                            usdt_balance = b['free']
                            break
                    
                    position_pct = self.config.agents.get('risk', {}).get('max_position_percent', 1)
                    amount = str(usdt_balance * position_pct / 100)
                    
                    if float(amount) > 10:  # Minimum $10
                        result = self.execute_spot_buy(signal.symbol, amount, 'binance')
                        self.save_trade_to_db(signal.id, result)
                        results.append(result)
                
                elif signal.signal_type == 'SELL':
                    # Need to check if we have the asset
                    # For now, skip sell execution (would need position tracking)
                    self.log('info', f"SELL signal for {signal.symbol} — position tracking needed")
                    self.update_signal_status(signal.id, 'SKIPPED')
            
            except Exception as e:
                self.log('error', f"Execution failed for signal {signal.id}: {e}")
        
        return {
            'timestamp': datetime.utcnow().isoformat(),
            'executed': len(results),
            'results': results,
        }
    
    def generate_confirmation_card(self, operation: Dict) -> str:
        """Generate structured confirmation card for Mainnet operations"""
        return f"""
[{'MAINNET' if not self.config.binance.get('testnet') else 'TESTNET'}] Operation Summary
--------------------------
Action:     {operation.get('side', 'BUY')}
Symbol:     {operation.get('symbol', '')}
Exchange:   {operation.get('exchange', 'binance')}
Quantity:   {operation.get('amount', 'N/A')}
Price:      Market
Est. Value: ~{operation.get('amount', 'N/A')} USDT
--------------------------
Type CONFIRM to execute or anything else to cancel.
"""
