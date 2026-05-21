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
        
        # Scalping parameters (futures)
        self.scalping_leverage = 15
        self.scalping_sl_pct = 0.5   # 0.5% stop loss
        self.scalping_tp_pct = 1.2   # 1.2% take profit
        self.scalping_trailing_activation_pct = 0.3  # activate trailing at 0.3% profit
        
        # Dynamic position sizing: min 1%, max 5% of balance
        self.min_position_pct = risk_cfg.get('min_position_percent', 1.0)
        self.max_position_pct_dynamic = risk_cfg.get('max_position_percent', 5.0)
        self.min_confidence_for_max_size = risk_cfg.get('min_confidence_for_max_size', 0.8)
    
    def calculate_position_size(self, confidence: float, exchange: str = 'bybit', market_type: str = 'spot') -> float:
        """
        Calculate position size based on signal confidence.
        
        Spot: $3-$10 per trade, up to 30% of balance
        Linear: 15-25% of balance as margin, scaled by confidence (2x leverage)
        """
        usdt_balance = self.get_usdt_balance(exchange)
        
        if market_type == 'linear':
            leverage = self.scalping_leverage
            min_margin = 3.0    # $3 min margin
            max_pct = 0.30      # 30% of balance as margin
            
            # For small balances (< $50), use higher percentage
            if usdt_balance < 50:
                # 50% at low confidence, 60% at high confidence
                max_pct_small = 0.60
                pct = 0.50 + (confidence - 0.35) / 0.65 * (max_pct_small - 0.50)
                pct = max(0.40, min(max_pct_small, pct))
                amount = usdt_balance * pct
            else:
                # Larger balances: scale between min_margin and 30% of balance
                max_margin = usdt_balance * max_pct
                if confidence <= 0.35:
                    amount = min_margin
                elif confidence >= 0.95:
                    amount = max_margin
                else:
                    amount = min_margin + (confidence - 0.35) / 0.60 * (max_margin - min_margin)
                amount = max(min_margin, amount)
            # Return notional value (margin × leverage) for info only, actual qty calculated in execute_linear_position
        else:
            min_trade = 3.0
            max_trade = 10.0
            max_pct = 0.30
            
            if confidence <= 0.35:
                amount = min_trade
            elif confidence >= 1.0:
                amount = max_trade
            else:
                amount = min_trade + (confidence - 0.35) / 0.65 * (max_trade - min_trade)
        
        return round(amount, 2)
    
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
    
    def get_open_position(self, symbol: str, exchange: str = 'bybit', market_type: str = 'spot') -> Optional[Position]:
        """Get open position for a symbol"""
        with self.db.get_session() as session:
            return session.query(Position).filter_by(
                symbol=symbol, exchange=exchange, market_type=market_type, status='OPEN'
            ).first()
    
    def get_all_open_positions(self, market_type: str = None) -> List[Dict]:
        """Get all open positions as dicts to avoid DetachedInstanceError.
        
        Args:
            market_type: If specified, filter by 'spot' or 'linear'. None = all.
        """
        with self.db.get_session() as session:
            query = session.query(Position).filter_by(status='OPEN')
            if market_type:
                query = query.filter_by(market_type=market_type)
            positions = query.all()
            # Detach from session by converting to dicts
            return [
                {
                    'id': p.id,
                    'symbol': p.symbol,
                    'exchange': p.exchange,
                    'market_type': p.market_type,
                    'side': p.side,
                    'entry_price': float(p.entry_price) if p.entry_price else 0,
                    'quantity': float(p.quantity) if p.quantity else 0,
                    'stop_loss': float(p.stop_loss) if p.stop_loss else None,
                    'take_profit': float(p.take_profit) if p.take_profit else None,
                    'trailing_stop_activated': p.trailing_stop_activated,
                    'trailing_stop_price': float(p.trailing_stop_price) if p.trailing_stop_price else None,
                    'highest_price': float(p.highest_price) if p.highest_price else None,
                    'lowest_price': float(p.lowest_price) if p.lowest_price else None,
                }
                for p in positions
            ]
    
    def create_position(self, symbol: str, exchange: str, entry_price: float,
                        quantity: float, cost_usdt: float, signal_id: Optional[int] = None,
                        trade_id: Optional[int] = None, 
                        sl_percent: Optional[float] = None,
                        tp_percent: Optional[float] = None,
                        market_type: str = 'spot',
                        side: str = 'LONG',
                        leverage: int = 1) -> Position:
        """Create a new position after BUY execution"""
        sl_pct = sl_percent or self.default_sl_pct
        tp_pct = tp_percent or self.default_tp_pct
        
        stop_loss = entry_price * (1 - sl_pct / 100)
        take_profit = entry_price * (1 + tp_pct / 100)
        
        with self.db.get_session() as session:
            position = Position(
                symbol=symbol,
                exchange=exchange,
                side=side,
                market_type=market_type,
                entry_price=Decimal(str(entry_price)),
                quantity=Decimal(str(quantity)),
                cost_usdt=Decimal(str(cost_usdt)),
                stop_loss=Decimal(str(stop_loss)),
                take_profit=Decimal(str(take_profit)),
                highest_price=Decimal(str(entry_price)),
                lowest_price=Decimal(str(entry_price)),
                signal_id=signal_id,
                trade_id=trade_id,
                leverage=leverage,
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
                
                # Trailing stop logic - use scalping parameters for linear
                if self.trailing_stop_enabled and not pos.trailing_stop_activated:
                    activation_pct = self.scalping_trailing_activation_pct if pos.market_type == 'linear' else self.trailing_activation_pct
                    if pnl_pct >= activation_pct:
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
                account = data['result']['list'][0]
                total_avail = float(account.get('totalAvailableBalance') or 0)
                total_equity = float(account.get('totalEquity') or 0)
                balances = account['coin']
                usdt_avail = total_avail
                usdt_total = float(account.get('totalWalletBalance') or 0)
                result_balances = [{'asset': 'USDT', 'free': usdt_avail, 'total': usdt_total, 'available': usdt_avail}]
                for b in balances:
                    if float(b.get('walletBalance', 0) or 0) > 0:
                        free_val = float(b.get('availableToWithdraw') or 0) if b.get('availableToWithdraw', '') != '' else float(b.get('walletBalance', 0))
                        avail_val = float(b.get('availableToWithdraw') or 0) if b.get('availableToWithdraw', '') != '' else 0
                        result_balances.append({
                            'asset': b['coin'],
                            'free': free_val,
                            'total': float(b.get('walletBalance') or 0),
                            'available': avail_val
                        })
                return {'exchange': exchange, 'balances': result_balances}
            
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
        """Get USDT balance for trading.
        
        For Bybit Unified account, uses totalAvailableBalance which 
        reflects all assets (BTC/ETH/USDT) usable as margin.
        For other exchanges, falls back to USDT-specific balance.
        """
        try:
            if exchange == 'bybit':
                ex = self.exchanges.get(exchange)
                if ex:
                    data = ex.get_wallet_balance('UNIFIED')
                    total_available = float(data['result']['list'][0].get('totalAvailableBalance', 0))
                    if total_available > 0:
                        return total_available
            # Fallback to per-asset USDT balance
            balance = self.get_balance(exchange)
            if 'error' in balance:
                return 0.0
            for b in balance.get('balances', []):
                if b['asset'] == 'USDT':
                    return b.get('available', b.get('free', 0))
        except Exception:
            pass
        return 0.0

    def cleanup_stale_orders(self, exchange: str = 'bybit', max_age_hours: int = 24) -> Dict:
        """Cancel all open orders older than max_age_hours (stale cleanup)"""
        result = {'cancelled': 0, 'errors': 0, 'details': []}
        ex = self.exchanges.get(exchange)
        if not ex:
            return {'error': f'Unknown exchange: {exchange}'}

        try:
            open_orders = ex.get_open_orders(category='spot')
            if open_orders.get('retCode') != 0:
                return {'error': open_orders.get('retMsg', 'Unknown error')}

            orders = open_orders.get('result', {}).get('list', [])
            if not orders:
                return result

            self.log('info', f"Found {len(orders)} open orders on {exchange}")

            # Cancel all open orders (aggressive cleanup)
            if exchange == 'bybit':
                # Cancel by settleCoin to clear all USDT orders
                cancel_result = ex.cancel_all_orders(category='spot')
                if cancel_result.get('retCode') == 0:
                    cancelled_list = cancel_result.get('result', {}).get('list', [])
                    result['cancelled'] = len(cancelled_list)
                    for o in cancelled_list:
                        result['details'].append(f"Cancelled orderId={o.get('orderId')}")
                    self.log('info', f"Cleaned up {result['cancelled']} stale orders on {exchange}")
                else:
                    result['errors'] = 1
                    self.log('error', f"Failed to cancel orders: {cancel_result}")
            else:
                # Per-symbol cancellation for other exchanges
                symbols = set(o.get('symbol') for o in orders)
                for symbol in symbols:
                    cancel_r = ex.cancel_all_orders(category='spot', symbol=symbol)
                    if cancel_r.get('retCode') == 0:
                        result['cancelled'] += len(cancel_r.get('result', {}).get('list', []))
                    else:
                        result['errors'] += 1

        except Exception as e:
            self.log('error', f"cleanup_stale_orders failed: {e}")
            result['errors'] += 1

        return result

    def _round_bybit_qty(self, symbol: str, quote_amount: str, exchange: str = 'bybit') -> str:
        """
        Round quantity to Bybit step size for spot market buy.
        Fetches instrument info and rounds to the nearest qty step.
        """
        try:
            ex = self.exchanges.get(exchange)
            if not ex:
                return quote_amount
            
            info = ex.get_instruments_info(category='spot', symbol=symbol)
            if info.get('retCode') != 0:
                self.log('warning', f"get_instruments_info failed: {info.get('retMsg')}")
                return quote_amount
            
            result = info.get('result', {})
            if not result or 'list' not in result or not result['list']:
                return quote_amount
            
            lot_filter = result['list'][0].get('lotSizeFilter', {})
            step = float(lot_filter.get('basePrecision', lot_filter.get('qtyStep', '1')))
            
            # Calculate qty from quote amount and current price
            ticker = ex.get_ticker(symbol)
            price = float(ticker.get('lastPrice', 0))
            if price <= 0:
                return quote_amount
            
            # With market_unit='quoteCoin', qty is in USDT (quote currency)
            # so we just return the quote_amount rounded to 2 decimal places
            qty_str = f"{float(quote_amount):.2f}"
            self.log('debug', f"_round_bybit_qty: {symbol} quote={quote_amount} → qty={qty_str} (market_unit=quoteCoin)")
            return qty_str
        except Exception as e:
            self.log('warning', f"_round_bybit_qty failed: {e}")
            return quote_amount

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
                # Round qty to Bybit step size
                qty = self._round_bybit_qty(symbol, quote_amount, exchange='bybit')
                result = ex.create_spot_buy(symbol, qty)
                order_id = result['result'].get('orderId')
                self.log('info', f"Bybit BUY {symbol}: {quote_amount} USDT → qty={qty} → orderId={order_id}")
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

    def execute_linear_position(self, symbol: str, side: str, amount_usdt: str,
                                 exchange: str = 'bybit', leverage: int = None) -> Dict:
        """Execute linear (futures) long or short position.
        
        Args:
            symbol: Trading symbol (e.g. 'BTCUSDT')
            side: 'long' or 'short'
            amount_usdt: USDT amount for position (margin, notional = margin × leverage)
            exchange: Exchange name
            leverage: Leverage multiplier (default: scalping_leverage=15)
        """
        if leverage is None:
            leverage = self.scalping_leverage
            
        try:
            ex = self.exchanges.get(exchange)
            if not ex:
                return {'exchange': exchange, 'symbol': symbol, 'side': side, 'error': f'No exchange: {exchange}'}
            
            # Get current price for qty calculation
            ticker = ex.get_ticker(symbol)
            price = float(ticker.get('lastPrice', 0))
            if price <= 0:
                return {'exchange': exchange, 'symbol': symbol, 'side': side, 'error': 'Could not get price'}
            
            # Calculate qty in base currency (USDT margin / price = qty)
            notional = float(amount_usdt) * leverage
            qty = notional / price
            
            # Round to appropriate precision
            if symbol.startswith('BTC'):
                qty = round(qty, 3)  # 3 decimal places for BTC pairs
            elif symbol.startswith('ETH'):
                qty = round(qty, 2)  # 2 decimal places for ETH pairs (step 0.01)
            else:
                qty = round(qty, 1)  # 1 decimal for other pairs
            
            # Ensure minimum viable qty
            min_qty = 0.001 if symbol.startswith('BTC') else (0.01 if symbol.startswith('ETH') else 1.0)
            if qty < min_qty:
                qty = min_qty
            
            if qty <= 0:
                return {'exchange': exchange, 'symbol': symbol, 'side': side, 'error': 'Qty <= 0'}
            
            qty_str = str(qty)
            leverage_str = str(leverage)
            
            # Calculate SL/TP using scalping parameters (0.5% SL, 1.2% TP)
            sl_pct = self.scalping_sl_pct / 100
            tp_pct = self.scalping_tp_pct / 100
            sl_price = round(price * (1 - sl_pct), 2) if side == 'long' else round(price * (1 + sl_pct), 2)
            tp_price = round(price * (1 + tp_pct), 2) if side == 'long' else round(price * (1 - tp_pct), 2)
            
            # Place order directly (bypass create_linear_long which calls set_leverage)
            if side == 'long':
                result = ex.create_order(
                    category='linear', symbol=symbol, side='Buy',
                    order_type='Market', qty=qty_str,
                )
            else:
                result = ex.create_order(
                    category='linear', symbol=symbol, side='Sell',
                    order_type='Market', qty=qty_str,
                )
            
            if result.get('retCode') != 0 and result.get('retCode') != 0:
                return {'exchange': exchange, 'symbol': symbol, 'side': side,
                        'error': f"Bybit API Error {result.get('retCode')}: {result.get('retMsg')}"}
            
            order_id = result.get('result', {}).get('orderId') or result.get('orderId', 'unknown')
            self.log('info', f"Bybit LINEAR {side.upper()} {symbol}: {amount_usdt} USDT × {leverage}x → qty={qty_str}, "
                      f"price={price}, SL={sl_price}, TP={tp_price} → orderId={order_id}")
            
            return {
                'exchange': exchange,
                'symbol': symbol,
                'side': side.upper(),
                'order_id': str(order_id),
                'status': 'FILLED',
                'executed_qty': str(qty),
                'price': str(price),
                'sl': str(sl_price),
                'tp': str(tp_price),
                'leverage': leverage_str,
            }
        
        except Exception as e:
            self.log('error', f"Linear position failed on {exchange}: {e}")
            return {'exchange': exchange, 'symbol': symbol, 'side': side, 'error': str(e)}

    def close_linear_position(self, symbol: str, side: str, qty: str,
                               exchange: str = 'bybit') -> Dict:
        """Close a linear (futures) position by opening opposite side with reduceOnly.
        
        Args:
            symbol: Trading symbol (e.g. 'BTCUSDT')
            side: Current position side ('LONG' or 'SHORT')
            qty: Position quantity to close
            exchange: Exchange name
        """
        try:
            ex = self.exchanges.get(exchange)
            if not ex:
                return {'exchange': exchange, 'symbol': symbol, 'error': f'No exchange: {exchange}'}
            
            # Close by taking opposite side
            close_side = 'Sell' if side.upper() == 'LONG' else 'Buy'
            
            result = ex.create_order(
                category='linear',
                symbol=symbol,
                side=close_side,
                order_type='Market',
                qty=qty,
                reduce_only=True,
            )
            
            if result.get('retCode') != 0:
                return {'exchange': exchange, 'symbol': symbol, 'side': close_side,
                        'error': f"Bybit API Error {result.get('retCode')}: {result.get('retMsg')}"}
            
            order_id = result.get('result', {}).get('orderId', 'unknown')
            self.log('info', f"Bybit LINEAR CLOSE {symbol} {side}: qty={qty} → orderId={order_id}")
            
            return {
                'exchange': exchange,
                'symbol': symbol,
                'side': close_side,
                'order_id': str(order_id),
                'status': 'FILLED',
                'executed_qty': qty,
            }
        
        except Exception as e:
            self.log('error', f"Linear close failed on {exchange}: {e}")
            return {'exchange': exchange, 'symbol': symbol, 'error': str(e)}

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
    
    def run_once(self, market_type: str = 'spot') -> Dict[str, Any]:
        """Execute pending signals and manage open positions.
        
        Args:
            market_type: 'spot' or 'linear' (futures)
        """
        self.log('info', f"Starting execution cycle ({market_type})...")
        
        results = {
            'timestamp': datetime.utcnow().isoformat(),
            'market_type': market_type,
            'buys_executed': 0,
            'sells_executed': 0,
            'sl_tp_triggered': 0,
            'errors': 0,
            'details': [],
        }
        
        # 1. Check SL/TP for open positions (same market type)
        sl_tp_result = self._check_and_execute_sl_tp(market_type)
        results['sl_tp_triggered'] = sl_tp_result['triggered']
        results['details'].extend(sl_tp_result['details'])
        
        # 2. Execute pending BUY signals for this market type
        buy_result = self._execute_pending_buys(market_type)
        results['buys_executed'] = buy_result['executed']
        results['errors'] += buy_result['errors']
        results['details'].extend(buy_result['details'])
        
        # 3. Execute pending SELL signals for this market type
        sell_result = self._execute_pending_sells(market_type)
        results['sells_executed'] = sell_result['executed']
        results['errors'] += sell_result['errors']
        results['details'].extend(sell_result['details'])
        
        self.log('info', f"Execution complete: {results['buys_executed']} buys, "
                  f"{results['sells_executed']} sells, {results['sl_tp_triggered']} SL/TP triggers")
        
        return results
    
    def _check_and_execute_sl_tp(self, market_type: str = 'spot') -> Dict:
        """Check and execute SL/TP for all open positions"""
        triggered = 0
        details = []
        
        open_positions = self.get_all_open_positions(market_type=market_type)
        
        for pos in open_positions:
            try:
                # Get current price from exchange
                exchange = pos['exchange']
                symbol = pos['symbol']
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
                    # Execute the close
                    qty = str(pos['quantity'])
                    side = pos.get('side', 'LONG')
                    if market_type == 'linear':
                        result = self.close_linear_position(symbol, side, qty, exchange)
                    else:
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
                self.log('error', f"SL/TP check failed for {pos.get('symbol', '?')}: {e}")
        
        return {'triggered': triggered, 'details': details}
    
    def _execute_pending_buys(self, market_type: str = 'spot') -> Dict:
        """Execute pending BUY signals"""
        executed = 0
        errors = 0
        details = []
        
        with self.db.get_session() as session:
            pending_buys_raw = session.query(Signal).filter_by(
                signal_type='BUY', status='PENDING', market_type=market_type
            ).order_by(Signal.created_at.desc()).limit(5).all()
            # Extract data while session is open to avoid DetachedInstanceError
            pending_buys = [
                {'id': s.id, 'symbol': s.symbol, 'exchange': s.exchange or 'bybit',
                 'confidence': s.confidence, 'created_at': s.created_at, 'market_type': market_type}
                for s in pending_buys_raw
            ]

        for sdata in pending_buys:
            try:
                signal_id = sdata['id']
                symbol = sdata['symbol']
                exchange = sdata['exchange']
                signal_confidence = float(sdata['confidence'] or 0.35)
                created_at = sdata['created_at']

                # Check if we already have a position for this market type
                existing = self.get_open_position(symbol, exchange, market_type)
                if existing:
                    self.log('info', f"Skipping BUY {symbol} ({market_type}) — already have open position")
                    self.update_signal_status(signal_id, 'SKIPPED')
                    continue

                # Execute buy: spot uses quote qty, linear uses base qty
                amount = self.calculate_position_size(signal_confidence, exchange, market_type)
                if amount < 3:
                    self.log('warning', f"Position size too small for {symbol}: ${amount:.2f}")
                    self.update_signal_status(signal_id, 'SKIPPED')
                    continue
                amount_str = str(round(amount, 2))
                if market_type == 'linear':
                    result = self.execute_linear_position(symbol, 'long', amount_str, exchange)
                else:
                    result = self.execute_spot_buy(symbol, amount_str, exchange)

                if 'error' in result:
                    errors += 1
                    self.log('error', f"BUY failed for {symbol}: {result['error']}")
                    self.update_signal_status(signal_id, 'FAILED')
                    continue

                # Save trade
                trade_id = self.save_trade_to_db(signal_id, result)

                # Get entry price
                if market_type == 'linear':
                    entry_price = float(result.get('price', 0))
                    executed_qty = float(result.get('executed_qty', 0))
                    cost = entry_price * executed_qty
                    leverage = int(result.get('leverage', 2))
                    pos_side = result.get('side', 'LONG')
                else:
                    executed_qty = float(result.get('executed_qty', 0))
                    cost = float(result.get('cummulative_quote_qty', amount))
                    entry_price = cost / executed_qty if executed_qty > 0 else 0
                    leverage = 1
                    pos_side = 'LONG'

                # Create position with SL/TP
                self.create_position(
                    symbol=symbol,
                    exchange=exchange,
                    entry_price=entry_price,
                    quantity=executed_qty,
                    cost_usdt=cost,
                    signal_id=signal_id,
                    trade_id=trade_id,
                    market_type=market_type,
                    side=pos_side,
                    leverage=leverage,
                )

                executed += 1
                self.log('info', f"BUY {symbol} | conf={signal_confidence:.0%} | ${amount:.2f} ({amount/self.get_usdt_balance(exchange)*100:.1f}% balance)")
                details.append({
                    'type': 'BUY',
                    'symbol': symbol,
                    'price': entry_price,
                    'quantity': executed_qty,
                    'cost': cost,
                    'confidence': signal_confidence,
                })

            except Exception as e:
                errors += 1
                self.log('error', f"Execution failed for signal {signal_id}: {e}")
        
        return {'executed': executed, 'errors': errors, 'details': details}
    
    def _execute_pending_sells(self, market_type: str = 'spot') -> Dict:
        """Execute pending SELL signals"""
        executed = 0
        errors = 0
        details = []
        
        with self.db.get_session() as session:
            pending_sells_raw = session.query(Signal).filter_by(
                signal_type='SELL', status='PENDING', market_type=market_type
            ).order_by(Signal.created_at.desc()).limit(5).all()
            pending_sells = [
                {'id': s.id, 'symbol': s.symbol, 'exchange': s.exchange or 'bybit',
                 'confidence': s.confidence, 'created_at': s.created_at, 'market_type': market_type}
                for s in pending_sells_raw
            ]

        for sdata in pending_sells:
            try:
                signal_id = sdata['id']
                symbol = sdata['symbol']
                exchange = sdata['exchange']
                signal_confidence = float(sdata['confidence'] or 0.35)
                created_at = sdata['created_at']

                # Find open position
                position = self.get_open_position(symbol, exchange, market_type)
                if not position:
                    self.log('info', f"Skipping SELL for {symbol} — no open position")
                    self.update_signal_status(signal_id, 'SKIPPED')
                    continue

                # Get current price
                ex = self.exchanges.get(exchange)
                ticker = ex.get_ticker(symbol)
                current_price = float(ticker.get('lastPrice', 0))

                # Execute close
                qty = str(float(position.quantity))
                pos_side = str(position.side) if hasattr(position, 'side') else 'LONG'
                if market_type == 'linear':
                    result = self.close_linear_position(symbol, pos_side, qty, exchange)
                else:
                    result = self.execute_spot_sell(symbol, qty, exchange)

                if 'error' in result:
                    errors += 1
                    self.log('error', f"SELL failed for {symbol}: {result['error']}")
                    self.update_signal_status(signal_id, 'FAILED')
                    continue

                # Close position
                close_result = self.close_position(
                    position.id, current_price, 'SIGNAL'
                )

                # Save trade
                self.save_trade_to_db(signal_id, {
                    **result,
                    'price': current_price,
                })

                self.update_signal_status(signal_id, 'EXECUTED',
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
                self.log('error', f"SELL execution failed for signal {signal_id}: {e}")
        
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
