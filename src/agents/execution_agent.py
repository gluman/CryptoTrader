import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
from decimal import Decimal
from .base import BaseAgent
from ..core.config import Config
from ..core.database import DatabaseManager, Signal, Trade, Position
from ..gateways import BinanceAPI, BybitAPI, BitfinexAPI


class ExecutionAgent(BaseAgent):
    """Executes trading decisions with position tracking and SL/TP"""
    
    def __init__(self, config: Config, logger: logging.Logger, db: DatabaseManager):
        super().__init__('Execution', logger)
        self.config = config
        self.db = db
        self.exchanges = {}
        self._init_exchanges()
        self.testnet_first = config.agents.get('executor', {}).get('testnet_first', True)
        self.confirmation_required = config.agents.get('executor', {}).get('confirmation_required', True)
        
        # Risk parameters
        risk_cfg = config.agents.get('risk', {})
        self.max_position_pct = risk_cfg.get('max_position_percent', 5)
        self.default_sl_pct = risk_cfg.get('default_stop_loss_percent', 2.0)
        self.default_tp_pct = risk_cfg.get('default_take_profit_percent', 4.0)
        self.risk_reward_ratio = risk_cfg.get('risk_reward_ratio', 2.0)
        self.trailing_stop_enabled = risk_cfg.get('trailing_stop_enabled', True)
        self.trailing_activation_pct = risk_cfg.get('trailing_stop_activation_percent', 1.5)
        self.trailing_distance_pct = risk_cfg.get('trailing_stop_distance_percent', 1.0)
    
    def _init_exchanges(self):
        """Initialize exchange connections"""
        if self.config.binance and self.config.binance.get('api_key'):
            self.exchanges['binance'] = BinanceAPI(
                api_key=self.config.binance['api_key'],
                api_secret=self.config.binance['api_secret'],
                testnet=self.config.binance.get('testnet', False),
                logger=self.logger
            )
        
        if self.config.bybit and self.config.bybit.get('api_key'):
            self.exchanges['bybit'] = BybitAPI(
                api_key=self.config.bybit['api_key'],
                api_secret=self.config.bybit['api_secret'],
                testnet=self.config.bybit.get('testnet', False),
                logger=self.logger
            )
        
        if self.config.bitfinex and self.config.bitfinex.get('api_key'):
            self.exchanges['bitfinex'] = BitfinexAPI(
                api_key=self.config.bitfinex['api_key'],
                api_secret=self.config.bitfinex['api_secret'],
                testnet=self.config.bitfinex.get('testnet', False),
                logger=self.logger
            )
    
    # ==================== Position Management ====================
    
    def get_open_position(self, symbol: str, exchange: str = 'binance') -> Optional[Position]:
        """Get open position for a symbol"""
        with self.db.get_session() as session:
            return session.query(Position).filter_by(
                symbol=symbol, exchange=exchange, status='OPEN'
            ).first()
    
    def get_all_open_positions(self) -> List[Position]:
        """Get all open positions"""
        with self.db.get_session() as session:
            return session.query(Position).filter_by(status='OPEN').all()
    
    def create_position(self, symbol: str, exchange: str, entry_price: float,
                        quantity: float, cost_usdt: float, signal_id: Optional[int] = None,
                        trade_id: Optional[int] = None, 
                        sl_percent: Optional[float] = None,
                        tp_percent: Optional[float] = None) -> Position:
        """Create a new position after BUY execution"""
        sl_pct = sl_percent or self.default_sl_pct
        tp_pct = tp_percent or self.default_tp_pct
        
        stop_loss = entry_price * (1 - sl_pct / 100)
        take_profit = entry_price * (1 + tp_pct / 100)
        
        with self.db.get_session() as session:
            position = Position(
                symbol=symbol,
                exchange=exchange,
                side='LONG',
                entry_price=Decimal(str(entry_price)),
                quantity=Decimal(str(quantity)),
                cost_usdt=Decimal(str(cost_usdt)),
                stop_loss=Decimal(str(stop_loss)),
                take_profit=Decimal(str(take_profit)),
                highest_price=Decimal(str(entry_price)),
                lowest_price=Decimal(str(entry_price)),
                signal_id=signal_id,
                trade_id=trade_id,
            )
            session.add(position)
            session.flush()
            position_id = position.id
        
        self.log('info', f"Position opened: {symbol} LONG {quantity:.6f} @ ${entry_price:.4f} | SL=${stop_loss:.4f} TP=${take_profit:.4f}")
        return position_id
    
    def close_position(self, position_id: int, close_price: float, reason: str = 'SIGNAL') -> Dict:
        """Close a position (SELL execution)"""
        with self.db.get_session() as session:
            pos = session.query(Position).filter_by(id=position_id).first()
            if not pos:
                return {'error': f'Position {position_id} not found'}
            
            entry = float(pos.entry_price)
            qty = float(pos.quantity)
            
            pnl_absolute = (close_price - entry) * qty
            pnl_percent = ((close_price - entry) / entry) * 100
            
            pos.status = 'CLOSED'
            pos.closed_at = datetime.utcnow()
            pos.close_price = Decimal(str(close_price))
            pos.realized_pnl = Decimal(str(pnl_absolute))
            pos.realized_pnl_percent = Decimal(str(pnl_percent))
            pos.updated_at = datetime.utcnow()
            pos.notes = f"Closed: {reason}"
            
            self.log('info', f"Position closed: {pos.symbol} PnL=${pnl_absolute:.2f} ({pnl_percent:.2f}%) reason={reason}")
            
            return {
                'position_id': position_id,
                'symbol': pos.symbol,
                'pnl_absolute': pnl_absolute,
                'pnl_percent': pnl_percent,
                'reason': reason,
            }
    
    def update_position_prices(self, symbol: str, current_price: float):
        """Update position PnL and trailing stop based on current price"""
        with self.db.get_session() as session:
            positions = session.query(Position).filter_by(
                symbol=symbol, status='OPEN'
            ).all()
            
            for pos in positions:
                entry = float(pos.entry_price)
                qty = float(pos.quantity)
                
                # Update PnL
                pnl = (current_price - entry) * qty
                pnl_pct = ((current_price - entry) / entry) * 100
                pos.unrealized_pnl = Decimal(str(pnl))
                pos.unrealized_pnl_percent = Decimal(str(pnl_pct))
                
                # Update high/low tracking
                if pos.highest_price is None or current_price > float(pos.highest_price):
                    pos.highest_price = Decimal(str(current_price))
                if pos.lowest_price is None or current_price < float(pos.lowest_price):
                    pos.lowest_price = Decimal(str(current_price))
                
                # Trailing stop logic
                if self.trailing_stop_enabled and not pos.trailing_stop_activated:
                    if pnl_pct >= self.trailing_activation_pct:
                        trailing_price = current_price * (1 - self.trailing_distance_pct / 100)
                        pos.trailing_stop_activated = True
                        pos.trailing_stop_price = Decimal(str(trailing_price))
                        self.log('info', f"Trailing stop activated for {symbol} @ ${trailing_price:.4f}")
                
                if pos.trailing_stop_activated:
                    new_trailing = current_price * (1 - self.trailing_distance_pct / 100)
                    if new_trailing > float(pos.trailing_stop_price):
                        pos.trailing_stop_price = Decimal(str(new_trailing))
                
                pos.updated_at = datetime.utcnow()
    
    def check_stop_loss_take_profit(self, symbol: str, current_price: float) -> List[Dict]:
        """Check if any positions hit SL or TP levels"""
        triggers = []
        
        with self.db.get_session() as session:
            positions = session.query(Position).filter_by(
                symbol=symbol, status='OPEN'
            ).all()
            
            for pos in positions:
                # Check stop loss
                if pos.stop_loss and current_price <= float(pos.stop_loss):
                    triggers.append({
                        'position_id': pos.id,
                        'symbol': symbol,
                        'type': 'STOP_LOSS',
                        'trigger_price': float(pos.stop_loss),
                        'current_price': current_price,
                    })
                    continue
                
                # Check trailing stop
                if pos.trailing_stop_activated and pos.trailing_stop_price:
                    if current_price <= float(pos.trailing_stop_price):
                        triggers.append({
                            'position_id': pos.id,
                            'symbol': symbol,
                            'type': 'TRAILING_STOP',
                            'trigger_price': float(pos.trailing_stop_price),
                            'current_price': current_price,
                        })
                        continue
                
                # Check take profit
                if pos.take_profit and current_price >= float(pos.take_profit):
                    triggers.append({
                        'position_id': pos.id,
                        'symbol': symbol,
                        'type': 'TAKE_PROFIT',
                        'trigger_price': float(pos.take_profit),
                        'current_price': current_price,
                    })
        
        return triggers
    
    # ==================== Balance ====================
    
    def get_balance(self, exchange: str = 'binance') -> Dict:
        """Get account balance"""
        try:
            ex = self.exchanges.get(exchange)
            if not ex:
                return {'exchange': exchange, 'balances': [], 'error': f'Unknown exchange: {exchange}'}
            
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
    
    def get_usdt_balance(self, exchange: str = 'binance') -> float:
        """Get USDT balance for trading"""
        balance = self.get_balance(exchange)
        for b in balance.get('balances', []):
            if b['asset'] == 'USDT':
                return b['free']
        return 0.0
    
    # ==================== Order Execution ====================
    
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
                sell_amount = f"-{quantity}"
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
    
    # ==================== Database Operations ====================
    
    def save_trade_to_db(self, signal_id: Optional[int], result: Dict) -> int:
        """Save executed trade to database and return trade_id"""
        with self.db.get_session() as session:
            trade = Trade(
                signal_id=signal_id,
                exchange=result.get('exchange', 'unknown'),
                symbol=result.get('symbol', ''),
                side=result.get('side', ''),
                order_type='MARKET',
                quantity=float(result.get('executed_qty', 0)),
                price=float(result.get('price', 0)) if result.get('price') else None,
                order_id=result.get('order_id'),
                created_at=datetime.utcnow(),
            )
            session.add(trade)
            session.flush()
            trade_id = trade.id
            
            if signal_id:
                signal = session.query(Signal).filter_by(id=signal_id).first()
                if signal:
                    signal.status = 'EXECUTED'
                    signal.executed_at = datetime.utcnow()
        
        return trade_id
    
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
    
    # ==================== Main Run Loop ====================
    
    def run_once(self) -> Dict[str, Any]:
        """Execute pending signals and manage open positions"""
        self.log('info', "Starting execution cycle...")
        
        results = {
            'timestamp': datetime.utcnow().isoformat(),
            'buys_executed': 0,
            'sells_executed': 0,
            'sl_tp_triggered': 0,
            'errors': 0,
            'details': [],
        }
        
        # 1. Check SL/TP for open positions
        sl_tp_result = self._check_and_execute_sl_tp()
        results['sl_tp_triggered'] = sl_tp_result['triggered']
        results['details'].extend(sl_tp_result['details'])
        
        # 2. Execute pending BUY signals
        buy_result = self._execute_pending_buys()
        results['buys_executed'] = buy_result['executed']
        results['errors'] += buy_result['errors']
        results['details'].extend(buy_result['details'])
        
        # 3. Execute pending SELL signals
        sell_result = self._execute_pending_sells()
        results['sells_executed'] = sell_result['executed']
        results['errors'] += sell_result['errors']
        results['details'].extend(sell_result['details'])
        
        self.log('info', f"Execution complete: {results['buys_executed']} buys, "
                  f"{results['sells_executed']} sells, {results['sl_tp_triggered']} SL/TP triggers")
        
        return results
    
    def _check_and_execute_sl_tp(self) -> Dict:
        """Check and execute SL/TP for all open positions"""
        triggered = 0
        details = []
        
        open_positions = self.get_all_open_positions()
        
        for pos in open_positions:
            try:
                # Get current price from exchange
                exchange = pos.exchange
                symbol = pos.symbol
                ex = self.exchanges.get(exchange)
                
                if not ex:
                    continue
                
                ticker = ex.get_ticker(symbol)
                current_price = float(ticker.get('lastPrice', 0))
                
                if current_price <= 0:
                    continue
                
                # Update position prices and check triggers
                self.update_position_prices(symbol, current_price)
                triggers = self.check_stop_loss_take_profit(symbol, current_price)
                
                for trigger in triggers:
                    # Execute the sell
                    qty = str(float(pos.quantity))
                    result = self.execute_spot_sell(symbol, qty, exchange)
                    
                    if 'error' not in result:
                        close_reason = trigger['type']
                        close_result = self.close_position(
                            trigger['position_id'], current_price, close_reason
                        )
                        triggered += 1
                        details.append({
                            'type': close_reason,
                            'symbol': symbol,
                            'price': current_price,
                            'pnl': close_result.get('pnl_absolute', 0),
                            'pnl_pct': close_result.get('pnl_percent', 0),
                        })
                        
                        # Save trade
                        trade_result = self.save_trade_to_db(None, {
                            **result,
                            'price': current_price,
                        })
                        
                        self.log('info', f"{close_reason} triggered for {symbol} @ ${current_price:.4f}")
            
            except Exception as e:
                self.log('error', f"SL/TP check failed for {pos.symbol}: {e}")
        
        return {'triggered': triggered, 'details': details}
    
    def _execute_pending_buys(self) -> Dict:
        """Execute pending BUY signals"""
        executed = 0
        errors = 0
        details = []
        
        with self.db.get_session() as session:
            pending_buys = session.query(Signal).filter_by(
                signal_type='BUY', status='PENDING'
            ).order_by(Signal.created_at.desc()).limit(5).all()
        
        for signal in pending_buys:
            try:
                symbol = signal.symbol
                exchange = signal.exchange or 'binance'
                
                # Check if we already have a position
                existing = self.get_open_position(symbol, exchange)
                if existing:
                    self.log('info', f"Skipping BUY for {symbol} — already have open position")
                    self.update_signal_status(signal.id, 'SKIPPED')
                    continue
                
                # Calculate position size
                usdt_balance = self.get_usdt_balance(exchange)
                amount = usdt_balance * self.max_position_pct / 100
                
                if amount < 10:  # Minimum $10
                    self.log('warning', f"Insufficient balance for {symbol}: ${amount:.2f}")
                    self.update_signal_status(signal.id, 'SKIPPED')
                    continue
                
                # Execute buy
                amount_str = str(round(amount, 2))
                result = self.execute_spot_buy(symbol, amount_str, exchange)
                
                if 'error' in result:
                    errors += 1
                    self.log('error', f"BUY failed for {symbol}: {result['error']}")
                    self.update_signal_status(signal.id, 'FAILED')
                    continue
                
                # Save trade
                trade_id = self.save_trade_to_db(signal.id, result)
                
                # Get entry price
                executed_qty = float(result.get('executed_qty', 0))
                cost = float(result.get('cummulative_quote_qty', amount))
                entry_price = cost / executed_qty if executed_qty > 0 else 0
                
                # Create position with SL/TP
                self.create_position(
                    symbol=symbol,
                    exchange=exchange,
                    entry_price=entry_price,
                    quantity=executed_qty,
                    cost_usdt=cost,
                    signal_id=signal.id,
                    trade_id=trade_id,
                )
                
                executed += 1
                details.append({
                    'type': 'BUY',
                    'symbol': symbol,
                    'price': entry_price,
                    'quantity': executed_qty,
                    'cost': cost,
                })
            
            except Exception as e:
                errors += 1
                self.log('error', f"Execution failed for signal {signal.id}: {e}")
        
        return {'executed': executed, 'errors': errors, 'details': details}
    
    def _execute_pending_sells(self) -> Dict:
        """Execute pending SELL signals"""
        executed = 0
        errors = 0
        details = []
        
        with self.db.get_session() as session:
            pending_sells = session.query(Signal).filter_by(
                signal_type='SELL', status='PENDING'
            ).order_by(Signal.created_at.desc()).limit(5).all()
        
        for signal in pending_sells:
            try:
                symbol = signal.symbol
                exchange = signal.exchange or 'binance'
                
                # Find open position
                position = self.get_open_position(symbol, exchange)
                if not position:
                    self.log('info', f"Skipping SELL for {symbol} — no open position")
                    self.update_signal_status(signal.id, 'SKIPPED')
                    continue
                
                # Get current price
                ex = self.exchanges.get(exchange)
                ticker = ex.get_ticker(symbol)
                current_price = float(ticker.get('lastPrice', 0))
                
                # Execute sell
                qty = str(float(position.quantity))
                result = self.execute_spot_sell(symbol, qty, exchange)
                
                if 'error' in result:
                    errors += 1
                    self.log('error', f"SELL failed for {symbol}: {result['error']}")
                    self.update_signal_status(signal.id, 'FAILED')
                    continue
                
                # Close position
                close_result = self.close_position(
                    position.id, current_price, 'SIGNAL'
                )
                
                # Save trade
                self.save_trade_to_db(signal.id, {
                    **result,
                    'price': current_price,
                })
                
                self.update_signal_status(signal.id, 'EXECUTED', 
                                          close_result.get('pnl_percent'))
                
                executed += 1
                details.append({
                    'type': 'SELL',
                    'symbol': symbol,
                    'price': current_price,
                    'quantity': float(qty),
                    'pnl': close_result.get('pnl_absolute', 0),
                    'pnl_pct': close_result.get('pnl_percent', 0),
                })
            
            except Exception as e:
                errors += 1
                self.log('error', f"SELL execution failed for signal {signal.id}: {e}")
        
        return {'executed': executed, 'errors': errors, 'details': details}
    
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
