import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
from decimal import Decimal
from .base import BaseAgent
from ..core.config import Config
from ..core.database import DatabaseManager, Signal, Trade, Position, StrategySignal
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
    
    def get_all_open_positions(self) -> List[Dict]:
        """Get all open positions as dicts (avoids DetachedInstanceError)"""
        with self.db.get_session() as session:
            positions = session.query(Position).filter_by(status='OPEN').all()
            # Extract all needed fields while session is open
            return [
                {
                    'id': p.id,
                    'symbol': p.symbol,
                    'exchange': p.exchange,
                    'quantity': p.quantity,
                    'entry_price': p.entry_price,
                    'stop_loss': p.stop_loss,
                    'take_profit': p.take_profit,
                    'market_type': p.market_type,
                    'trailing_stop_activated': p.trailing_stop_activated,
                    'trailing_stop_price': p.trailing_stop_price,
                    'highest_price': p.highest_price,
                    'lowest_price': p.lowest_price,
                }
                for p in positions
            ]
    
    def create_position(self, symbol: str, exchange: str, entry_price: float,
                        quantity: float, cost_usdt: float, signal_id: Optional[int] = None,
                        trade_id: Optional[int] = None,
                        sl_percent: Optional[float] = None,
                        tp_percent: Optional[float] = None,
                        market_type: str = 'spot') -> Optional[int]:
        """Create a new position after BUY execution"""
        sl_pct = sl_percent or self.default_sl_pct
        tp_pct = tp_percent or self.default_tp_pct
        
        stop_loss = entry_price * (1 - sl_pct / 100)
        take_profit = entry_price * (1 + tp_pct / 100)
        
        # Validate quantity — reject zero/negative
        if quantity <= 0:
            self.log('warning', f"Skipping position create: {symbol} qty={quantity} <= 0")
            return None

        # Validate cost
        if cost_usdt <= 0:
            self.log('warning', f"Skipping position create: {symbol} cost={cost_usdt} <= 0")
            return None

        with self.db.get_session() as session:
            position = Position(
                symbol=symbol.upper(),
                exchange=exchange.upper(),
                side='LONG',
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
                # Response: result.list = [{account_info_with 'coin': [coins]}]
                account_coins = data['result']['list'][0]['coin']
                return {'exchange': exchange, 'balances': [
                    {'asset': b['coin'],
                     'free': float(b['availableToWithdraw']) if b.get('availableToWithdraw') else float(b.get('walletBalance') or 0),
                     'total': float(b.get('walletBalance') or 0)}
                    for b in account_coins if float(b.get('walletBalance') or 0) > 0
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
    
    def save_trade_to_db(self, signal_id: Optional[int], result: Dict, position_id: Optional[int] = None) -> int:
        """Save executed trade to database and return trade_id"""
        # Calculate PnL if we have entry and close prices
        pnl_abs = 0.0
        pnl_pct = 0.0
        if result.get('price') and result.get('entry_price'):
            entry = float(result['entry_price'])
            close = float(result['price'])
            qty = float(result.get('executed_qty', 0) or result.get('quantity', 0))
            pnl_abs = (close - entry) * qty
            pnl_pct = ((close - entry) / entry) * 100 if entry > 0 else 0.0

        with self.db.get_session() as session:
            trade = Trade(
                signal_id=signal_id,
                exchange=result.get('exchange', 'unknown').upper(),
                symbol=result.get('symbol', '').upper(),
                side=result.get('side', '').upper(),
                order_type='MARKET',
                quantity=float(result.get('executed_qty', 0) or 0),
                price=float(result.get('price', 0)) if result.get('price') else None,
                fee=0,
                pnl_percent=Decimal(str(round(pnl_pct, 4))),
                pnl_absolute=Decimal(str(round(pnl_abs, 4))),
                stop_loss=Decimal(str(result['stop_loss'])) if result.get('stop_loss') else None,
                take_profit=Decimal(str(result['take_profit'])) if result.get('take_profit') else None,
                order_id=result.get('order_id'),
                created_at=datetime.utcnow(),
            )
            session.add(trade)
            session.flush()
            trade_id = trade.id
            
            if signal_id:
                sig = session.query(StrategySignal).filter_by(id=signal_id).first()
                if sig:
                    sig.status = 'executed'
                    sig.executed_at = datetime.utcnow()
        
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
    
    def run_once(self, market_type: str = 'linear') -> Dict[str, Any]:
        """Execute pending signals and manage open positions"""
        self.log('info', f"Starting execution cycle ({market_type})...")

        results = {
            'timestamp': datetime.utcnow().isoformat(),
            'buys_executed': 0,
            'sells_executed': 0,
            'sl_tp_triggered': 0,
            'signals_cleaned': 0,
            'trailing_updated': 0,
            'errors': 0,
            'details': [],
        }

        # 0. Clean expired signals (TTL by strategy)
        clean_result = self._clean_expired_signals()
        results['signals_cleaned'] = clean_result['count']
        results['details'].extend(clean_result['details'])

        # 1. Check SL/TP for open positions and update trailing
        sl_tp_result = self._check_and_execute_sl_tp()
        results['sl_tp_triggered'] = sl_tp_result['triggered']
        results['trailing_updated'] = sl_tp_result.get('trailing_updated', 0)
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
                  f"{results['sells_executed']} sells, {results['sl_tp_triggered']} SL/TP, "
                  f"{results['signals_cleaned']} cleaned")

        return results
    
    def _clean_expired_signals(self) -> Dict:
        """Delete pending signals that exceeded their TTL (by strategy)."""
        from datetime import timedelta
        # TTL by strategy (in minutes)
        ttl_by_strategy = {
            'scalping': 15,
            'intraday': 60,
            'position': 240,
        }
        cleaned = 0
        details = []

        with self.db.get_session() as session:
            for strategy, ttl_min in ttl_by_strategy.items():
                cutoff = datetime.utcnow() - timedelta(minutes=ttl_min)
                deleted = session.query(StrategySignal).filter(
                    StrategySignal.strategy == strategy,
                    StrategySignal.status == 'pending',
                    StrategySignal.created_at < cutoff,
                ).delete()
                if deleted > 0:
                    cleaned += deleted
                    details.append(f"{strategy}: {deleted} expired")

            session.commit()

        return {'count': cleaned, 'details': details}

    def _check_and_execute_sl_tp(self) -> Dict:
        """Check and execute SL/TP for all open positions.
        Also updates trailing stop on Bybit if price moved favorably."""
        triggered = 0
        trailing_updated = 0
        details = []

        open_positions = self.get_all_open_positions()

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

                # Update position prices (also updates local trailing state)
                self.update_position_prices(symbol, current_price)
                triggers = self.check_stop_loss_take_profit(symbol, current_price)

                # === Trailing stop update on Bybit ===
                if pos['market_type'] == 'linear' and self.trailing_stop_enabled:
                    try:
                        entry_price = float(pos.get('entry_price', 0))
                        if entry_price > 0:
                            pnl_pct = (current_price - entry_price) / entry_price * 100
                            pos_obj = None
                            # Get the position object to check trailing state
                            with self.db.get_session() as sess:
                                from src.core.database import Position
                                db_pos = sess.query(Position).filter_by(
                                    symbol=symbol, status='OPEN'
                                ).first()
                                if db_pos:
                                    trailing_activated = db_pos.trailing_stop_activated
                                    trailing_dist = self.trailing_distance_pct
                                    trailing_activation = self.trailing_activation_pct

                                    if not trailing_activated and pnl_pct >= trailing_activation:
                                        # Activate trailing: set new SL closer to price
                                        new_sl = round(current_price * (1 - trailing_dist / 100), 4)
                                        ts_result = ex.set_position_sl(
                                            symbol=symbol,
                                            stop_loss=str(new_sl),
                                            trailing_active=True,
                                            trailing_distance=str(trailing_dist),
                                        )
                                        if ts_result.get('retCode') == 0:
                                            db_pos.trailing_stop_activated = True
                                            db_pos.trailing_stop_price = Decimal(str(new_sl))
                                            sess.commit()
                                            self.log('info', f"Trailing ACTIVATED for {symbol} @ ${new_sl:.4f} (pnl={pnl_pct:.2f}%)")
                                            trailing_updated += 1

                                    elif trailing_activated:
                                        # Move trailing SL up if price moved up
                                        current_trailing_sl = float(db_pos.trailing_stop_price or 0)
                                        new_trailing_sl = round(current_price * (1 - trailing_dist / 100), 4)
                                        if new_trailing_sl > current_trailing_sl:
                                            ts_result = ex.set_position_sl(
                                                symbol=symbol,
                                                stop_loss=str(new_trailing_sl),
                                                trailing_active=True,
                                                trailing_distance=str(trailing_dist),
                                            )
                                            if ts_result.get('retCode') == 0:
                                                db_pos.trailing_stop_price = Decimal(str(new_trailing_sl))
                                                sess.commit()
                                                self.log('info', f"Trailing MOVED for {symbol}: ${current_trailing_sl:.4f} → ${new_trailing_sl:.4f}")
                                                trailing_updated += 1
                    except Exception as ts_err:
                        self.log('warning', f"Trailing update error for {symbol}: {ts_err}")

                # === Execute triggered SL/TP ===
                for trigger in triggers:
                    qty = float(pos['quantity'])
                    if pos['market_type'] == 'linear':
                        result = self.execute_linear_sell(symbol, qty, exchange)
                    else:
                        result = self.execute_spot_sell(symbol, str(qty), exchange)

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

                        trade_result = self.save_trade_to_db(None, {
                            **result,
                            'price': current_price,
                            'entry_price': float(pos['entry_price']),
                        })

                        self.log('info', f"{close_reason} triggered for {symbol} @ ${current_price:.4f}")

            except Exception as e:
                self.log('error', f"SL/TP check failed for {pos['symbol']}: {e}")

        return {'triggered': triggered, 'trailing_updated': trailing_updated, 'details': details}
    
    def _execute_pending_buys(self) -> Dict:
        """Execute pending BUY signals from strategy_signals"""
        executed = 0
        errors = 0
        details = []

        # Track which symbols we've already opened this cycle
        opened_this_cycle = set()

        with self.db.get_session() as session:
            # No limit — process all pending signals, dedupe by symbol
            pending_buys_raw = session.query(StrategySignal).filter_by(
                action='BUY', status='pending'
            ).order_by(StrategySignal.created_at.desc()).all()
            pending_buys = [
                {'id': s.id, 'symbol': s.symbol, 'exchange': s.exchange or 'bybit',
                 'confidence': s.confidence, 'strategy': s.strategy,
                 'stop_loss': s.stop_loss, 'take_profit': s.take_profit}
                for s in pending_buys_raw
            ]

        for sdata in pending_buys:
            try:
                symbol = sdata['symbol']
                exchange = sdata['exchange']
                signal_id = sdata['id']

                # Skip if we already opened this symbol this cycle
                if symbol in opened_this_cycle:
                    self._update_strategy_signal_status(signal_id, 'skipped')
                    continue

                # Check Bybit directly for open positions (not DB — may be stale)
                # Count how many open positions we have for this symbol
                max_per_symbol = getattr(self.config.risk, 'max_positions_per_symbol', 1)
                ex = self.exchanges.get(exchange)
                open_count = 0
                if ex and exchange == 'bybit':
                    try:
                        pos_resp = ex.get_positions(category='linear', symbol=symbol)
                        pos_list = pos_resp.get('result', {}).get('list', [])
                        open_count = sum(1 for p in pos_list if float(p.get('size', 0)) > 0)
                    except Exception:
                        open_count = 0
                else:
                    open_count = 1 if self.get_open_position(symbol, exchange) else 0

                if open_count >= max_per_symbol:
                    self.log('info', f"Skipping BUY {symbol} — {open_count} open position(s) on {exchange} (max={max_per_symbol})")
                    self._update_strategy_signal_status(signal_id, 'skipped')
                    continue

                usdt_balance = self.get_usdt_balance(exchange)
                amount = usdt_balance  # 100% balance, leverage calculated in execute_linear_buy

                if amount < 5:
                    self.log('warning', f"Balance too small: ${amount:.2f}")
                    self._update_strategy_signal_status(signal_id, 'skipped')
                    continue

                amount_str = str(round(amount, 2))

                # Extract SL/TP from signal if available
                sl_pct = None
                tp_pct = None
                if sdata.get('stop_loss') and sdata.get('entry_price'):
                    try:
                        entry = float(sdata['entry_price'])
                        sl = float(sdata['stop_loss'])
                        if entry > 0:
                            sl_pct = abs((sl - entry) / entry) * 100
                    except (TypeError, ValueError):
                        pass
                if sdata.get('take_profit') and sdata.get('entry_price'):
                    try:
                        entry = float(sdata['entry_price'])
                        tp = float(sdata['take_profit'])
                        if entry > 0:
                            tp_pct = abs((tp - entry) / entry) * 100
                    except (TypeError, ValueError):
                        pass

                result = self.execute_linear_buy(symbol, amount_str, exchange, sl_pct, tp_pct)

                if 'error' in result:
                    errors += 1
                    self.log('error', f"BUY failed {symbol}: {result['error']}")
                    self._update_strategy_signal_status(signal_id, 'failed')
                    continue

                executed_qty = float(result.get('executed_qty', 0) or 0)
                cost = float(result.get('cummulative_quote_qty', amount))
                entry_price = cost / executed_qty if executed_qty > 0 else 0

                # Skip if quantity is zero
                if executed_qty <= 0:
                    self.log('warning', f"Executed qty=0 for {symbol} — skipping position")
                    self._update_strategy_signal_status(signal_id, 'skipped')
                    errors += 1
                    continue

                # Save trade AFTER we have entry_price and executed_qty
                result['entry_price'] = entry_price
                result['stop_loss'] = sdata.get('stop_loss')
                result['take_profit'] = sdata.get('take_profit')
                trade_id = self.save_trade_to_db(signal_id, result)

                position_id = self.create_position(
                    symbol=symbol,
                    exchange=exchange,
                    market_type='linear',
                    entry_price=entry_price,
                    quantity=executed_qty,
                    cost_usdt=cost,
                    signal_id=signal_id,
                    trade_id=trade_id,
                )

                if position_id is None:
                    self.log('warning', f"Position create returned None for {symbol} — skipping")
                    self._update_strategy_signal_status(signal_id, 'skipped')
                    errors += 1
                    continue

                # Mark as executed
                self._update_strategy_signal_status(signal_id, 'executed')
                opened_this_cycle.add(symbol)
                executed += 1
                details.append({
                    'type': 'BUY',
                    'symbol': symbol,
                    'price': entry_price,
                    'quantity': executed_qty,
                    'cost': cost,
                    'strategy': sdata.get('strategy', 'scalping'),
                })

            except Exception as e:
                errors += 1
                self.log('error', f"Execution failed signal {signal_id}: {e}")

        return {'executed': executed, 'errors': errors, 'details': details}

    def _update_strategy_signal_status(self, signal_id: int, status: str):
        try:
            with self.db.get_session() as session:
                sig = session.query(StrategySignal).get(signal_id)
                if sig:
                    sig.status = status
                    session.commit()
        except Exception as e:
            self.log('error', f"Failed update signal {signal_id}: {e}")

    def execute_linear_buy(self, symbol: str, amount: str, exchange: str,
                           sl_pct: float = None, tp_pct: float = None) -> Dict:
        try:
            ex = self.exchanges.get(exchange)
            if not ex:
                return {'error': f'No exchange: {exchange}'}
            ticker = ex.get_ticker(symbol)
            current_price = float(ticker.get('lastPrice', 0))
            if current_price <= 0:
                return {'error': 'No price'}
            amount_usdt = float(amount)

            # Step 1: Calculate min leverage needed for min notional $5
            min_leverage = max(5.0 / amount_usdt, 1)
            leverage = int(min_leverage)

            # Step 2: Calculate qty with that leverage, round to lot size step (0.01)
            pos_value = amount_usdt * leverage
            qty = pos_value / current_price
            qty = round(qty / 0.01) * 0.01  # Round to qtyStep=0.01

            if qty < 0.01:
                return {'error': f'Qty {qty:.4f} below min 0.01'}

            # Step 2b: Calculate SL/TP prices
            if sl_pct is None:
                sl_pct = self.default_sl_pct
            if tp_pct is None:
                tp_pct = self.default_tp_pct

            stop_loss = str(round(current_price * (1 - sl_pct / 100), 4))
            take_profit = str(round(current_price * (1 + tp_pct / 100), 4))

            # Step 3: Open position WITH SL/TP on Bybit
            result = ex.create_linear_order(
                symbol=symbol, side='Buy', order_type='Market', qty=str(qty),
                leverage=str(leverage),
                stop_loss=stop_loss, take_profit=take_profit,
            )

            if 'error' in result:
                return result

            # Step 4: If trailing enabled, set trailing stop via Bybit set-trading-stop
            if self.trailing_stop_enabled:
                try:
                    import time; time.sleep(0.3)
                    trailing_dist = str(round(self.trailing_distance_pct, 2))
                    ts_result = ex.set_position_sl(
                        symbol=symbol,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        trailing_active=True,
                        trailing_distance=trailing_dist,
                    )
                    if ts_result.get('retCode') == 0:
                        self.log('info', f"Trailing stop set for {symbol}: dist={trailing_dist}%")
                    else:
                        self.log('warning', f"Trailing set failed: {ts_result}")
                except Exception as ts_err:
                    self.log('warning', f"Trailing stop error: {ts_err}")

            # Step 5: Fetch position to get actual filled qty and entry price
            try:
                import time; time.sleep(0.5)
                pos_resp = ex.get_positions(category='linear', symbol=symbol)
                pos_list = pos_resp.get('result', {}).get('list', [])
                for pos in pos_list:
                    if float(pos.get('size', 0)) > 0:
                        result['executed_qty'] = float(pos['size'])
                        result['avgPrice'] = float(pos.get('avgPrice') or current_price)
                        result['cummulative_quote_qty'] = result['executed_qty'] * result['avgPrice']
                        break
            except Exception:
                pass

            return result
        except Exception as e:
            return {'error': str(e)}

    def execute_linear_sell(self, symbol: str, quantity: float, exchange: str = 'bybit') -> Dict:
        """Execute linear futures market sell (close long position)"""
        try:
            ex = self.exchanges.get(exchange)
            if not ex:
                return {'error': f'No exchange: {exchange}'}
            result = ex.create_linear_order(
                symbol=symbol, side='Sell', order_type='Market', qty=str(quantity)
            )

            if 'error' in result:
                return result

            # Fetch position to get filled qty
            try:
                import time; time.sleep(0.5)
                pos_resp = ex.get_positions(category='linear', symbol=symbol)
                pos_list = pos_resp.get('result', {}).get('list', [])
                for pos in pos_list:
                    if float(pos.get('size', 0)) > 0:
                        result['executed_qty'] = float(pos['size'])
                        result['avgPrice'] = float(pos.get('avgPrice') or 0)
                        result['cummulative_quote_qty'] = result['executed_qty'] * result['avgPrice']
                        break
            except Exception:
                pass

            return result
        except Exception as e:
            return {'error': str(e)}

    def _execute_pending_sells(self) -> Dict:
        """Execute pending SELL signals from strategy_signals"""
        executed = 0
        errors = 0
        details = []

        # Extract data while session is open to avoid DetachedInstanceError
        with self.db.get_session() as session:
            pending_sells_raw = session.query(StrategySignal).filter_by(
                action='SELL', status='pending'
            ).order_by(StrategySignal.created_at.desc()).limit(5).all()
            pending_sells = [
                {'id': s.id, 'symbol': s.symbol, 'exchange': s.exchange or 'bybit'}
                for s in pending_sells_raw
            ]

        for sdata in pending_sells:
            try:
                symbol = sdata['symbol']
                exchange = sdata['exchange']
                signal_id = sdata['id']

                # Find open position
                position = self.get_open_position(symbol, exchange)
                if not position:
                    self.log('info', f"Skipping SELL for {symbol} — no open position")
                    self._update_strategy_signal_status(signal_id, 'skipped')
                    continue

                # Get current price
                ex = self.exchanges.get(exchange)
                ticker = ex.get_ticker(symbol)
                current_price = float(ticker.get('lastPrice', 0))

                # Execute sell — use linear sell for futures
                qty = float(position.quantity)
                if position.market_type == 'linear':
                    result = self.execute_linear_sell(symbol, qty, exchange)
                else:
                    result = self.execute_spot_sell(symbol, str(qty), exchange)

                if 'error' in result:
                    errors += 1
                    self.log('error', f"SELL failed for {symbol}: {result['error']}")
                    self._update_strategy_signal_status(signal_id, 'failed')
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

                self._update_strategy_signal_status(signal_id, 'executed')

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
