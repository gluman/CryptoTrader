import logging
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple
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
        # Fixed minimal position size (notional margin at `leverage`x). 2026-05-27 go-live minimal.
        self.position_usd = float(risk_cfg.get('max_position_usd', 5.0))
        self.leverage = int(risk_cfg.get('leverage', 1))
        self.default_sl_pct = risk_cfg.get('default_stop_loss_percent', 2.0)
        self.default_tp_pct = risk_cfg.get('default_take_profit_percent', 4.0)
        self.sl_atr_multiplier = risk_cfg.get('sl_atr_multiplier', 1.0)
        self.tp_atr_multiplier = risk_cfg.get('tp_atr_multiplier', 1.7)
        self.risk_reward_ratio = risk_cfg.get('risk_reward_ratio', 2.0)
        self.trailing_stop_enabled = risk_cfg.get('trailing_stop_enabled', True)
        self.trailing_activation_pct = risk_cfg.get('trailing_stop_activation_percent', 1.5)
        self.trailing_distance_pct = risk_cfg.get('trailing_stop_distance_percent', 1.0)
        # Scalp defenses (set in settings.yaml under agents.risk)
        # FIX [Tolya 2026-05-27]: max(1, ...) prevents 0-value which blocks ALL signals
        # (open_cnt >= 0 is always True when max_open=0, making position_cap unreachable)
        self.max_open_positions_total = max(1, risk_cfg.get('max_open_positions_total', 1))
        self.daily_max_loss_usd = risk_cfg.get('daily_max_loss_usd', 2.0)
        self.sl_cooldown_seconds = risk_cfg.get('sl_cooldown_seconds', 300)

    def _adaptive_sl_tp_pct(self, symbol: str, exchange: str = 'bybit') -> Tuple[float, float, str]:
        """Return (sl_pct, tp_pct, note) — ATR-based on latest 5m bars, with floor.
        Falls back to defaults if ATR unavailable."""
        try:
            from src.core.database import OHLCVRaw
            from sqlalchemy import desc
            with self.db.get_session() as sess:
                rows = (
                    sess.query(OHLCVRaw.high, OHLCVRaw.low, OHLCVRaw.close)
                    .filter(OHLCVRaw.exchange == exchange,
                            OHLCVRaw.symbol == symbol,
                            OHLCVRaw.timeframe == '5m')
                    .order_by(desc(OHLCVRaw.timestamp))
                    .limit(15).all()
                )
            if len(rows) < 15:
                return self.default_sl_pct, self.default_tp_pct, f"defaults (only {len(rows)} bars)"
            bars = list(reversed([(float(h), float(l), float(c)) for h, l, c in rows]))
            trs = []
            for i in range(1, len(bars)):
                h, l, _ = bars[i]
                pc = bars[i-1][2]
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr_abs = sum(trs[-14:]) / 14 if trs else 0
            price = bars[-1][2]
            if price <= 0 or atr_abs <= 0:
                return self.default_sl_pct, self.default_tp_pct, "defaults (no ATR)"
            atr_pct = (atr_abs / price) * 100
            sl_pct = max(self.default_sl_pct, self.sl_atr_multiplier * atr_pct)
            tp_pct = max(self.default_tp_pct, self.tp_atr_multiplier * atr_pct)
            return sl_pct, tp_pct, f"ATR_pct={atr_pct:.3f}% → SL={sl_pct:.3f}% TP={tp_pct:.3f}%"
        except Exception as e:
            return self.default_sl_pct, self.default_tp_pct, f"defaults (error: {e})"

    def _get_market_qty_step(self, symbol: str) -> float:
        """M4 helper (08.06.2026): получить qtyStep для пары через ccxt market().

        ════════════════════════════════════════════════════════════════════
        ЗАЧЕМ
        ════════════════════════════════════════════════════════════════════

        Bybit для каждой пары задаёт индивидуальный шаг количества
        (`lotSizeFilter.qtyStep`). Хардкод `0.01` в коде исполнения — баг,
        потому что:

          • DOGEUSDT: qtyStep = 1     → 0.01 → 0.01, потом Bybit скажет
            «too small / too much precision» и отклонит ордер (110017).
          • NEARUSDT, SUIUSDT, LITUSDT: qtyStep = 0.1
          • BTCUSDT, ETHUSDT, TONUSDT, SOLUSDT: qtyStep = 0.01 (тут ок)
          • ADAUSDT, MATICUSDT, WLDUSDT: qtyStep = 1

        Симптом: ордер отклоняется с 110017, partial-close TP не
        проходит, и стратегия зависает в `position.status='OPEN'`
        при фактическом отсутствии позиции на бирже.

        ════════════════════════════════════════════════════════════════════
        КАК
        ════════════════════════════════════════════════════════════════════

        1. Берём ccxt-инстанс Bybit (self._bybit() — singleton, time-sync
           через bybit_safe.py).
        2. `ccxt_b.market(symbol_ccxt)` возвращает dict с полем
           `info.lotSizeFilter.qtyStep` (raw от Bybit V5).
        3. Парсим `qtyStep` → float. Fallback на ccxt `precision.amount`
           (для пар, где ccxt не достал lotSizeFilter — старые пары).
        4. Final fallback: 0.01 (универсально совместимо с BTC/ETH/SOL).

        ════════════════════════════════════════════════════════════════════
        ПРИМЕНЯЕТСЯ В
        ════════════════════════════════════════════════════════════════════

          • execute_linear_buy:1312    — `qty = round(qty / step) * step`
          • execute_linear_short:1465  — то же
          • execute_linear_sell:855    — partial TP close
          • _bybit_market_buy_ccxt:1411 — legacy ccxt-путь

        Также добавлен strip trailing zeros (`0.10000 → 0.1`) для
        совместимости с Bybit (он принимает только нормализованные
        числа, иначе 170137).

        ════════════════════════════════════════════════════════════════════
        RETURNS
        ════════════════════════════════════════════════════════════════════

        float — qtyStep для данной пары. Например: 1.0, 0.1, 0.01.
        Никогда не возвращает 0 или None (минимум 0.01).

        См. code_review/CODE_REVIEW_2026-06-08.md, §0.4 R4.
        """
        try:
            ccxt_b = self._bybit()
            if ccxt_b is None:
                return 0.01
            # ccxt ждёт формат 'BASE/QUOTE:SETTLE' для linear:
            #   DOGEUSDT → DOGE/USDT:USDT
            sym_ccxt = symbol.upper().replace('USDT', '/USDT:USDT')
            m = ccxt_b.market(sym_ccxt)
            lsf = (m.get('info', {}) or {}).get('lotSizeFilter', {})
            step = float(lsf.get('qtyStep') or (m.get('precision', {}) or {}).get('amount') or 0.01)
            return step if step > 0 else 0.01
        except Exception:
            return 0.01

    def _today_realized_pnl(self) -> float:
        """Sum realized_pnl for positions closed since UTC midnight."""
        from sqlalchemy import func as sqlfunc
        try:
            with self.db.get_session() as session:
                today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                total = session.query(sqlfunc.coalesce(sqlfunc.sum(Position.realized_pnl), 0)).filter(
                    Position.status == 'CLOSED',
                    Position.closed_at >= today_start,
                ).scalar()
                return float(total or 0)
        except Exception as e:
            self.log('warning', f"daily_pnl query failed: {e}")
            return 0.0

    def _last_sl_close_age(self) -> Optional[float]:
        """Seconds since the most recent SL-style close (realized_pnl < 0). None if no such close exists."""
        try:
            with self.db.get_session() as session:
                last = session.query(Position).filter(
                    Position.status == 'CLOSED',
                    Position.realized_pnl < 0,
                ).order_by(Position.closed_at.desc()).first()
                if last is None or last.closed_at is None:
                    return None
                return (datetime.utcnow() - last.closed_at.replace(tzinfo=None)).total_seconds()
        except Exception as e:
            self.log('warning', f"sl_cooldown query failed: {e}")
            return None

    def _count_open_positions(self) -> int:
        try:
            with self.db.get_session() as session:
                return session.query(Position).filter(Position.status == 'OPEN').count()
        except Exception:
            return 0

    def _check_defenses_pre_open(self) -> Tuple[bool, str]:
        """Returns (allowed, reason). Apply all gates before opening any new position."""
        # 1. Daily loss halt
        pnl = self._today_realized_pnl()
        if pnl <= -abs(self.daily_max_loss_usd):
            return False, f"daily_loss_halt: today PnL ${pnl:.2f} ≤ -${self.daily_max_loss_usd:.2f}"

        # 2. Global open-position cap
        open_cnt = self._count_open_positions()
        if open_cnt >= self.max_open_positions_total:
            return False, f"position_cap: {open_cnt}/{self.max_open_positions_total} already open"

        # 3. SL cooldown
        age = self._last_sl_close_age()
        if age is not None and age < self.sl_cooldown_seconds:
            remaining = int(self.sl_cooldown_seconds - age)
            return False, f"sl_cooldown: {remaining}s remaining after last loss"

        return True, ""
    
    def _init_exchanges(self):
        """Initialize exchange connections"""
        # Bybit: use ccxt for exchange operations (handles signature correctly)
        if self.config.bybit and self.config.bybit.get('api_key'):
            import ccxt
            self.ccxt_bybit = ccxt.bybit({
                'apiKey': self.config.bybit['api_key'],
                'secret': self.config.bybit['api_secret'],
                'enableRateLimit': True,
                'adjustForTimeDifference': True,
                'options': {'defaultType': 'swap', 'defaultMarginMode': 'isolated', 'recvWindow': 60000}
            })
            # Sync local clock with Bybit server (critical for 10002 timestamp error)
            try:
                self.ccxt_bybit.load_time_difference()
            except Exception as e:
                self.log('warning', f'load_time_difference failed: {e}')
            # Test ccxt is working
            try:
                self.ccxt_bybit.fetch_balance(params={'type': 'swap', 'accountType': 'UNIFIED'})
                self.log('info', 'ccxt bybit initialized OK')
            except Exception as e:
                self.log('warning', f'ccxt bybit init failed: {e}')
                self.ccxt_bybit = None

        if self.config.binance and self.config.binance.get('api_key'):
            self.exchanges['binance'] = BinanceAPI(
                api_key=self.config.binance['api_key'],
                api_secret=self.config.binance['api_secret'],
                testnet=self.config.binance.get('testnet', False),
                logger=self.logger
            )

        if self.config.bitfinex and self.config.bitfinex.get('api_key'):
            self.exchanges['bitfinex'] = BitfinexAPI(
                api_key=self.config.bitfinex['api_key'],
                api_secret=self.config.bitfinex['api_secret'],
                testnet=self.config.bitfinex.get('testnet', False),
                logger=self.logger
            )

    def _bybit(self):
        """Get ccxt bybit instance (preferred) or BybitAPI fallback for bybit operations."""
        return getattr(self, 'ccxt_bybit', None)

    def _get_bybit_positions_ccxt(self, symbol: str = None) -> List[Dict]:
        """Get open positions from Bybit via ccxt (handles signature correctly).
        Returns list of position dicts with: symbol, side, quantity, entry_price, unrealizedPnl."""
        ccxt_bybit = self._bybit()
        if not ccxt_bybit:
            return []
        try:
            sym = symbol.upper().replace('USDT', '/USDT:USDT') if symbol else 'SOL/USDT:USDT'
            positions = ccxt_bybit.fetch_positions([sym])
            result = []
            for p in positions:
                if p['contracts'] and float(p['contracts']) > 0:
                    result.append({
                        'symbol': p['symbol'].replace('/USDT:USDT', 'USDT'),
                        'side': p['side'].upper() if p['side'] else None,
                        'quantity': float(p['contracts']),
                        'entry_price': float(p.get('entryPrice') or 0),
                        'unrealizedPnl': float(p.get('unrealizedPnl') or 0),
                    })
            return result
        except Exception as e:
            self.log('warning', f'_get_bybit_positions_ccxt failed: {e}')
            return []

    def _get_bybit_balance_ccxt(self) -> Dict[str, float]:
        """Get USDT balance from Bybit via ccxt. Returns {'free': float, 'used': float, 'total': float}."""
        ccxt_bybit = self._bybit()
        if not ccxt_bybit:
            return {'free': 0, 'used': 0, 'total': 0}
        try:
            bal = ccxt_bybit.fetch_balance(params={'type': 'swap', 'accountType': 'UNIFIED'})
            usdt = bal.get('USDT', {})
            return {'free': float(usdt.get('free', 0)), 'used': float(usdt.get('used', 0)), 'total': float(usdt.get('total', 0))}
        except Exception as e:
            self.log('warning', f'_get_bybit_balance_ccxt failed: {e}')
            return {'free': 0, 'used': 0, 'total': 0}

    def _bybit_market_buy_ccxt(self, symbol: str, amount_usdt: float) -> Dict:
        """Place market buy on Bybit via ccxt. Returns {'retCode': 0, 'orderId': ..., 'executed_qty': ..., 'avgPrice': ...}."""
        ccxt_bybit = self._bybit()
        if not ccxt_bybit:
            return {'error': 'ccxt not initialized'}
        try:
            # amount_usdt is USDT value, ccxt expects amount in base currency
            sym = symbol.upper().replace('USDT', '/USDT:USDT')
            market = ccxt_bybit.load_markets().get(sym, {})
            price = market.get('last') or market.get('ask') or 1
            qty = round(amount_usdt / price, 2)
            if qty < 0.01:
                return {'error': f'Qty {qty:.4f} below min 0.01'}
            order = ccxt_bybit.create_order(sym, 'market', 'buy', qty)
            return {
                'retCode': 0,
                'orderId': order.get('id'),
                'executed_qty': float(order.get('filled', qty)),
                'avgPrice': float(order.get('average', price)),
            }
        except Exception as e:
            return {'error': str(e)}

    def _bybit_market_sell_ccxt(self, symbol: str, quantity: float) -> Dict:
        """Place market sell on Bybit via ccxt. Returns {'retCode': 0, 'orderId': ...}."""
        ccxt_bybit = self._bybit()
        if not ccxt_bybit:
            return {'error': 'ccxt not initialized'}
        try:
            sym = symbol.upper().replace('USDT', '/USDT:USDT')
            order = ccxt_bybit.create_order(sym, 'market', 'sell', quantity)
            return {
                'retCode': 0,
                'orderId': order.get('id'),
                'executed_qty': float(order.get('filled', quantity)),
                'avgPrice': float(order.get('average', 0)),
            }
        except Exception as e:
            return {'error': str(e)}

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
                        market_type: str = 'spot',
                        side: str = 'LONG',
                        exit_plan: Optional[Dict] = None,
                        strategy: Optional[str] = None) -> Optional[int]:
        """Create a new position. exit_plan optional: {"tp1":{"price_pct":...,"close_qty_pct":...}, "tp2":...,"tp3":...,"sl":{},"max_hold_minutes":...}

        M2 (08.06.2026): параметр `strategy` пишется в `notes` для per-strategy dedup.
        """
        sl_pct = sl_percent or self.default_sl_pct
        tp_pct = tp_percent or self.default_tp_pct

        # exit_plan overrides sl/tp pct if provided
        if exit_plan:
            sl_node = exit_plan.get("sl") or {}
            if isinstance(sl_node, dict) and sl_node.get("price_pct"):
                sl_pct = float(sl_node["price_pct"])
            tp3_node = exit_plan.get("tp3") or {}
            if isinstance(tp3_node, dict) and tp3_node.get("price_pct"):
                tp_pct = float(tp3_node["price_pct"])

        if side.upper() == 'SHORT':
            stop_loss = entry_price * (1 + sl_pct / 100)
            take_profit = entry_price * (1 - tp_pct / 100)
        else:
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

        # Compute TP1/TP2/TP3 prices if exit_plan present
        tp1_price = tp2_price = tp3_price = None
        tp1_qty_pct = tp2_qty_pct = tp3_qty_pct = None
        max_hold_until = None
        is_short = (side.upper() == 'SHORT')
        if exit_plan:
            for level in ("tp1", "tp2", "tp3"):
                node = exit_plan.get(level) or {}
                if not isinstance(node, dict):
                    continue
                p_pct = node.get("price_pct")
                q_pct = node.get("close_qty_pct")
                if p_pct is None or q_pct is None:
                    continue
                price = entry_price * (1 - p_pct / 100) if is_short else entry_price * (1 + p_pct / 100)
                if level == "tp1":
                    tp1_price, tp1_qty_pct = price, q_pct
                elif level == "tp2":
                    # M5 fix: было `q_pct and price` — при q_pct=0 давало 0.
                    tp2_price, tp2_qty_pct = price, q_pct
                elif level == "tp3":
                    tp3_price, tp3_qty_pct = price, q_pct
            mhm = exit_plan.get("max_hold_minutes")
            if mhm:
                from datetime import timedelta as _td
                max_hold_until = datetime.utcnow() + _td(minutes=int(mhm))

        with self.db.get_session() as session:
            position = Position(
                symbol=symbol.upper(),
                exchange=exchange.upper(),
                side=side.upper(),
                market_type=market_type,
                entry_price=Decimal(str(entry_price)),
                quantity=Decimal(str(quantity)),
                initial_quantity=Decimal(str(quantity)),
                cost_usdt=Decimal(str(cost_usdt)),
                stop_loss=Decimal(str(stop_loss)),
                take_profit=Decimal(str(take_profit)),
                highest_price=Decimal(str(entry_price)),
                lowest_price=Decimal(str(entry_price)),
                signal_id=signal_id,
                trade_id=trade_id,
                notes=strategy,  # M2: для per-strategy dedup (clone5_multi_runner.has_open_position)
                exit_plan_json=exit_plan if exit_plan else None,
                tp1_price=Decimal(str(tp1_price)) if tp1_price else None,
                tp1_qty_pct=Decimal(str(tp1_qty_pct)) if tp1_qty_pct else None,
                tp2_price=Decimal(str(tp2_price)) if tp2_price else None,
                tp2_qty_pct=Decimal(str(tp2_qty_pct)) if tp2_qty_pct else None,
                tp3_price=Decimal(str(tp3_price)) if tp3_price else None,
                tp3_qty_pct=Decimal(str(tp3_qty_pct)) if tp3_qty_pct else None,
                max_hold_until=max_hold_until,
            )
            session.add(position)
            session.flush()
            position_id = position.id

        tp_summary = ""
        if tp1_price:
            tp_summary = f" | TP1=${float(tp1_price):.4f}({tp1_qty_pct}%) TP2=${float(tp2_price or 0):.4f}({tp2_qty_pct or 0}%) TP3=${float(tp3_price or 0):.4f}({tp3_qty_pct or 0}%)"
        self.log('info', f"Position opened: {symbol} {side.upper()} {quantity:.6f} @ ${entry_price:.4f} | SL=${stop_loss:.4f}{tp_summary}")
        return position_id
    
    def close_position(self, position_id: int, close_price: float, reason: str = 'SIGNAL') -> Dict:
        """Close a position (SELL execution)"""
        with self.db.get_session() as session:
            pos = session.query(Position).filter_by(id=position_id).first()
            if not pos:
                return {'error': f'Position {position_id} not found'}
            
            entry = float(pos.entry_price)
            qty = float(pos.quantity)
            side = (pos.side or 'LONG').upper()

            if side == 'SHORT':
                pnl_absolute = (entry - close_price) * qty
                pnl_percent = ((entry - close_price) / entry) * 100
            else:
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
                side = (pos.side or 'LONG').upper()

                if side == 'SHORT':
                    pnl = (entry - current_price) * qty
                    pnl_pct = ((entry - current_price) / entry) * 100
                else:
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
                side = (pos.side or 'LONG').upper()
                # SHORT: SL above entry, TP below — invert comparisons
                if side == 'SHORT':
                    if pos.stop_loss and current_price >= float(pos.stop_loss):
                        triggers.append({'position_id': pos.id, 'symbol': symbol,
                            'type': 'STOP_LOSS', 'trigger_price': float(pos.stop_loss),
                            'current_price': current_price})
                        continue
                    if pos.trailing_stop_activated and pos.trailing_stop_price and current_price >= float(pos.trailing_stop_price):
                        triggers.append({'position_id': pos.id, 'symbol': symbol,
                            'type': 'TRAILING_STOP', 'trigger_price': float(pos.trailing_stop_price),
                            'current_price': current_price})
                        continue
                    if pos.take_profit and current_price <= float(pos.take_profit):
                        triggers.append({'position_id': pos.id, 'symbol': symbol,
                            'type': 'TAKE_PROFIT', 'trigger_price': float(pos.take_profit),
                            'current_price': current_price})
                else:
                    # LONG
                    if pos.stop_loss and current_price <= float(pos.stop_loss):
                        triggers.append({'position_id': pos.id, 'symbol': symbol,
                            'type': 'STOP_LOSS', 'trigger_price': float(pos.stop_loss),
                            'current_price': current_price})
                        continue
                    if pos.trailing_stop_activated and pos.trailing_stop_price and current_price <= float(pos.trailing_stop_price):
                        triggers.append({'position_id': pos.id, 'symbol': symbol,
                            'type': 'TRAILING_STOP', 'trigger_price': float(pos.trailing_stop_price),
                            'current_price': current_price})
                        continue
                    if pos.take_profit and current_price >= float(pos.take_profit):
                        triggers.append({'position_id': pos.id, 'symbol': symbol,
                            'type': 'TAKE_PROFIT', 'trigger_price': float(pos.take_profit),
                            'current_price': current_price})
        
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
        # TTL by strategy (in minutes). Clone5 пишет раз в час, исполнитель подхватывает
        # каждые 3 мин — 60 мин достаточно.
        ttl_by_strategy = {
            'scalping': 15,
            'intraday': 60,
            'position': 240,
            'clone5_v7_trailing_only': 60,
            'clone5_v6_market_maker_full': 60,
            'clone5_v2_market_maker_ict': 60,
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

    def _check_and_execute_partial_tps(self, pos: Dict, current_price: float, ex) -> Dict:
        """Multi-level TP1/TP2/TP3 partial-close + max-hold time stop.

        On TP-level hit: market-close X% of *initial* quantity (so each level closes its
        designed share regardless of prior closes). Updates partial_closes_count and
        tp{N}_hit_at. After TP3 the position is fully closed via close_position."""
        from src.core.database import Position
        symbol = pos['symbol']
        side = (pos.get('side') or 'LONG').upper()
        is_short = side == 'SHORT'

        result = {'triggered': 0, 'details': []}
        try:
            with self.db.get_session() as sess:
                db_pos = sess.query(Position).filter_by(id=pos['id'], status='OPEN').first()
                if not db_pos:
                    return result

                init_qty = float(db_pos.initial_quantity or db_pos.quantity)
                if init_qty <= 0:
                    return result

                # max_hold expiry → close all
                if db_pos.max_hold_until and datetime.utcnow() > db_pos.max_hold_until.replace(tzinfo=None):
                    self.log('info', f"max_hold expired for {symbol} — closing all")
                    qty_left = float(db_pos.quantity)
                    if qty_left > 0:
                        close_res = self.execute_linear_sell(symbol, qty_left, pos['exchange'])
                        if 'error' not in close_res:
                            self.close_position(db_pos.id, current_price, 'MAX_HOLD')
                            result['triggered'] += 1
                            result['details'].append({'type': 'MAX_HOLD', 'symbol': symbol, 'price': current_price})
                    return result

                # TP levels — check in order: TP1, TP2, TP3
                for level_name, tp_price_col, tp_qty_col, tp_hit_col in [
                    ('TP1', 'tp1_price', 'tp1_qty_pct', 'tp1_hit_at'),
                    ('TP2', 'tp2_price', 'tp2_qty_pct', 'tp2_hit_at'),
                    ('TP3', 'tp3_price', 'tp3_qty_pct', 'tp3_hit_at'),
                ]:
                    tp_price = getattr(db_pos, tp_price_col)
                    tp_qty_pct = getattr(db_pos, tp_qty_col)
                    tp_hit_at = getattr(db_pos, tp_hit_col)
                    if not tp_price or not tp_qty_pct or tp_hit_at is not None:
                        continue  # missing or already hit
                    tp_price = float(tp_price)
                    if is_short:
                        triggered_now = current_price <= tp_price
                    else:
                        triggered_now = current_price >= tp_price
                    if not triggered_now:
                        continue

                    # Compute quantity to close (% of INITIAL qty)
                    close_qty = round(init_qty * float(tp_qty_pct) / 100, 4)
                    # M4 fix (08.06.2026): qtyStep из ccxt market() вместо хардкода 0.01
                    step = self._get_market_qty_step(symbol) if hasattr(self, '_get_market_qty_step') else 0.01
                    close_qty = round(close_qty / step) * step
                    close_qty = float(f"{close_qty:.10f}".rstrip('0').rstrip('.') or 0)
                    current_qty = float(db_pos.quantity)
                    close_qty = min(close_qty, current_qty)
                    if close_qty <= 0:
                        # Cannot partial-close (below min step) → mark as hit but skip
                        setattr(db_pos, tp_hit_col, datetime.utcnow())
                        sess.commit()
                        continue

                    # Execute partial close — reverse side of the position
                    if is_short:
                        close_res = ex.create_linear_order(
                            symbol=symbol, side='Buy', order_type='Market',
                            qty=str(close_qty), reduce_only=True,
                        )
                    else:
                        close_res = ex.create_linear_order(
                            symbol=symbol, side='Sell', order_type='Market',
                            qty=str(close_qty), reduce_only=True,
                        )
                    if 'error' in close_res:
                        self.log('warning', f"{level_name} partial close failed for {symbol}: {close_res['error']}")
                        continue

                    # Update DB
                    setattr(db_pos, tp_hit_col, datetime.utcnow())
                    db_pos.partial_closes_count = (db_pos.partial_closes_count or 0) + 1
                    new_qty = max(0, current_qty - close_qty)
                    db_pos.quantity = Decimal(str(new_qty))

                    # Realized PnL piece
                    entry = float(db_pos.entry_price)
                    if is_short:
                        piece_pnl = (entry - current_price) * close_qty
                    else:
                        piece_pnl = (current_price - entry) * close_qty
                    prev_pnl = float(db_pos.realized_pnl or 0)
                    db_pos.realized_pnl = Decimal(str(prev_pnl + piece_pnl))

                    # After TP1, raise SL to breakeven
                    if level_name == 'TP1':
                        db_pos.stop_loss = Decimal(str(entry))
                    # After TP2, raise SL to TP1 level (lock profit)
                    if level_name == 'TP2' and db_pos.tp1_price:
                        db_pos.stop_loss = db_pos.tp1_price

                    sess.commit()
                    self.log('info', f"{level_name} HIT for {side} {symbol} @ ${current_price:.4f} — closed {close_qty} ({float(tp_qty_pct)}%) pnl_piece=${piece_pnl:.4f}  remaining={new_qty:.4f}")
                    result['triggered'] += 1
                    result['details'].append({
                        'type': level_name, 'symbol': symbol, 'price': current_price,
                        'closed_qty': close_qty, 'pnl_piece': piece_pnl,
                    })

                    # If we just closed everything → finalize
                    if new_qty <= 0:
                        self.close_position(db_pos.id, current_price, level_name)
                        break
        except Exception as e:
            self.log('warning', f"partial_tps error for {pos.get('symbol')}: {e}")
        return result

    def _sync_orphan_positions(self) -> int:
        """Detect DB-open positions that no longer exist on Bybit (closed exchange-side)
        and sync them as CLOSED using closed-pnl endpoint. Returns count of synced.

        H6 fix (08.06.2026): используем Bybit server time для timestamp (вместо локального
        time.time() — у хоста часы могут уходить → 10002). Fallback на локальное время
        минус 1.5s (известный offset на 2026-06-08) если ccxt недоступен.
        """
        from src.core.database import Position
        import time as _t, hmac as _h, hashlib as _hh, requests as _r
        synced = 0
        try:
            with self.db.get_session() as sess:
                db_open = sess.query(Position).filter(
                    Position.status == 'OPEN',
                    Position.exchange == 'BYBIT',
                    Position.market_type == 'linear',
                ).all()
                if not db_open:
                    return 0
                bybit = self.exchanges.get('bybit')
                if not bybit:
                    return 0
                key = bybit.api_key; secret = bybit.api_secret

                def _bybit_timestamp() -> int:
                    """H6 fix (08.06.2026, см. §0.4 R3): серверное время Bybit для подписи.

                    ════════════════════════════════════════════════════════════════
                    ЗАЧЕМ
                    ════════════════════════════════════════════════════════════════

                    Bybit отклоняет подписанные запросы, у которых timestamp
                    опережает серверный более чем на recvWindow (10002). Хост
                    srv-cryptotrader дрейфует на ~1.5s вперёд (NTP отключён,
                    sudo у Босса). Старый «магический» fallback `-1500ms` —
                    хрупкий: при изменении дрейфа подпись ломалась молча.

                    Новая версия: 4-уровневый fallback БЕЗ магических констант,
                    всегда стараемся получить server time.

                    ════════════════════════════════════════════════════════════════
                    СТРАТЕГИЯ (от надёжного к менее надёжному)
                    ════════════════════════════════════════════════════════════════

                    1. `ccxt.get_server_time()` через `self._bybit()`
                       Лучший путь: ccxt сам дёрнет /v5/market/time и вернёт ms.
                       НО: счётчик квоты 1310 (OHLCV) НЕ затрагивается здесь,
                       но get_server_time может попасть под отдельный rate-limit.
                       Также при проблемах с сетью может дать 503/timeout.

                    2. `GET /v5/market/time` напрямую (без подписи)
                       Публичный endpoint, всегда работает, квоты 1310 нет.
                       Это — наша страховка: даже если ccxt сломан, мы
                       достучимся.

                    3. `ccxt.load_time_difference() + int(time.time()*1000)`
                       ccxt внутри запомнил `delta = serverTime - localTime`
                       при предыдущих вызовах. Возвращаем «уже скорректированное»
                       local time. Работает даже при сетевых проблемах.

                    4. `int(time.time() * 1000)` (БЕЗ коррекции)
                       Совсем плохой fallback. Если мы здесь — значит вообще
                       всё сломалось, и любой timestamp лучше чем ничего.
                       Bybit почти наверняка отклонит (10002), но мы хотя бы
                       залогируем и вернём 0 buys.

                    ════════════════════════════════════════════════════════════════
                    СМ. ТАКЖЕ
                    ════════════════════════════════════════════════════════════════

                      • bybit_safe.py:bybit_exchange() — для ccxt-путей
                      • cryptotrader_strategies/execute_cron.py — потребитель
                      • code_review/CODE_REVIEW_2026-06-08.md, §0.4 R3
                    """
                    # 1) ccxt.get_server_time (самый чистый путь)
                    try:
                        ccxt_b = self._bybit()
                        if ccxt_b:
                            ts = ccxt_b.get_server_time()
                            if ts:
                                return int(ts)
                    except Exception:
                        pass
                    # 2) Прямой GET /v5/market/time (без подписи, без квоты 1310)
                    try:
                        resp = _r.get('https://api.bybit.com/v5/market/time', timeout=5)
                        data = resp.json()
                        if data.get('retCode') == 0 and data.get('result', {}).get('timeSecond'):
                            return int(data['result']['timeSecond']) * 1000
                    except Exception:
                        pass
                    # 3) Local time + load_time_difference offset (ccxt-saved delta)
                    try:
                        ccxt_b = self._bybit()
                        if ccxt_b and hasattr(ccxt_b, 'load_time_difference'):
                            ccxt_b.load_time_difference()
                            # ccxt корректирует time.time() внутри при adjustForTimeDifference
                            return int(_t.time() * 1000)
                    except Exception:
                        pass
                    # 4) Совсем плохой fallback — local time. Без магических offset'ов.
                    return int(_t.time() * 1000)

                # Fetch all open linear positions on Bybit
                ts = str(_bybit_timestamp()); recv = '5000'
                q = 'category=linear&settleCoin=USDT'
                sig = _h.new(secret.encode(), (ts + key + recv + q).encode(), _hh.sha256).hexdigest()
                headers = {'X-BAPI-API-KEY': key, 'X-BAPI-TIMESTAMP': ts, 'X-BAPI-RECV-WINDOW': recv, 'X-BAPI-SIGN': sig}
                resp = _r.get('https://api.bybit.com/v5/position/list?' + q, headers=headers, timeout=10)
                live_syms = set()
                for p in resp.json().get('result', {}).get('list', []):
                    if float(p.get('size', 0)) > 0:
                        live_syms.add(p['symbol'])
                # For each DB-open not on Bybit → fetch closed-pnl and close
                for pos in db_open:
                    if pos.symbol in live_syms:
                        continue
                    age_s = (datetime.utcnow() - pos.opened_at.replace(tzinfo=None)).total_seconds()
                    if age_s < 30:
                        continue  # too new — wait for Bybit settlement
                    # Fetch latest closed-pnl record for this symbol
                    ts2 = str(_bybit_timestamp())
                    q2 = f'category=linear&symbol={pos.symbol}&limit=5'
                    sig2 = _h.new(secret.encode(), (ts2 + key + recv + q2).encode(), _hh.sha256).hexdigest()
                    h2 = {'X-BAPI-API-KEY': key, 'X-BAPI-TIMESTAMP': ts2, 'X-BAPI-RECV-WINDOW': recv, 'X-BAPI-SIGN': sig2}
                    r2 = _r.get('https://api.bybit.com/v5/position/closed-pnl?' + q2, headers=h2, timeout=10)
                    trades = r2.json().get('result', {}).get('list', [])
                    # Pick the trade whose entry matches our entry_price approximately
                    target = None
                    for t in trades:
                        try:
                            if abs(float(t['avgEntryPrice']) - float(pos.entry_price)) < float(pos.entry_price) * 0.001:
                                target = t
                                break
                        except (TypeError, KeyError, ValueError):
                            continue
                    if not target and trades:
                        target = trades[0]  # fallback to most recent
                    if not target:
                        self.log('warning', f"orphan {pos.symbol}#{pos.id}: no closed-pnl record")
                        continue
                    pnl = float(target['closedPnl'])
                    exit_p = float(target['avgExitPrice'])
                    pos.status = 'CLOSED'
                    pos.closed_at = datetime.utcnow()
                    pos.close_price = Decimal(str(exit_p))
                    pos.realized_pnl = Decimal(str(pnl))
                    if pos.entry_price and float(pos.entry_price) > 0:
                        entry_f = float(pos.entry_price)
                        pct = (exit_p - entry_f) / entry_f * 100
                        if (pos.side or 'LONG').upper() == 'SHORT':
                            pct = -pct
                        pos.realized_pnl_percent = Decimal(str(round(pct, 4)))
                    pos.notes = f"orphan-sync: Bybit closed orderId={target.get('orderId')[:12]}…  pnl=${pnl:.4f}"
                    pos.updated_at = datetime.utcnow()
                    synced += 1
                    self.log('info', f"orphan-sync: {pos.symbol}#{pos.id} {(pos.side or 'LONG')} closed @ ${exit_p:.4f} pnl=${pnl:.4f}")
                if synced:
                    sess.commit()
        except Exception as e:
            self.log('warning', f"orphan sync failed: {e}")
        return synced

    def _check_and_execute_sl_tp(self) -> Dict:
        """Check and execute SL/TP for all open positions.
        Also updates trailing stop on Bybit if price moved favorably."""
        triggered = 0
        trailing_updated = 0
        details = []

        # FIRST: sync orphans (Bybit-side closes that DB missed)
        synced = self._sync_orphan_positions()
        if synced:
            details.append({'type': 'ORPHAN_SYNC', 'count': synced})

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

                # === Multi-level TP partial-close + max-hold ===
                tp_partials = self._check_and_execute_partial_tps(pos, current_price, ex)
                if tp_partials:
                    triggered += tp_partials.get('triggered', 0)
                    details.extend(tp_partials.get('details', []))

                triggers = self.check_stop_loss_take_profit(symbol, current_price)

                # === Trailing stop update on Bybit (LONG and SHORT aware) ===
                # Bybit V5 trailingStop param is PRICE units (USDT), not percent.
                if pos['market_type'] == 'linear' and self.trailing_stop_enabled:
                    try:
                        entry_price = float(pos.get('entry_price', 0))
                        side = (pos.get('side') or 'LONG').upper()
                        if entry_price > 0:
                            if side == 'SHORT':
                                pnl_pct = (entry_price - current_price) / entry_price * 100
                            else:
                                pnl_pct = (current_price - entry_price) / entry_price * 100
                            with self.db.get_session() as sess:
                                from src.core.database import Position
                                db_pos = sess.query(Position).filter_by(
                                    symbol=symbol, status='OPEN'
                                ).first()
                                if db_pos:
                                    trailing_activated = db_pos.trailing_stop_activated
                                    trailing_dist_pct = self.trailing_distance_pct
                                    trailing_activation = self.trailing_activation_pct
                                    trailing_dist_price = round(current_price * (trailing_dist_pct / 100), 4)
                                    bybit_side = 'Sell' if side == 'SHORT' else 'Buy'

                                    if not trailing_activated and pnl_pct >= trailing_activation:
                                        if side == 'SHORT':
                                            new_sl = round(current_price * (1 + trailing_dist_pct / 100), 4)
                                        else:
                                            new_sl = round(current_price * (1 - trailing_dist_pct / 100), 4)
                                        ts_result = ex.set_position_sl(
                                            symbol=symbol, stop_loss=str(new_sl), side=bybit_side,
                                            trailing_active=True, trailing_distance=str(trailing_dist_price),
                                        )
                                        if ts_result.get('retCode') == 0:
                                            db_pos.trailing_stop_activated = True
                                            db_pos.trailing_stop_price = Decimal(str(new_sl))
                                            sess.commit()
                                            self.log('info', f"Trailing ACTIVATED for {side} {symbol} @ ${new_sl:.4f} (pnl={pnl_pct:.2f}%, dist=${trailing_dist_price})")
                                            trailing_updated += 1

                                    elif trailing_activated:
                                        current_trailing_sl = float(db_pos.trailing_stop_price or 0)
                                        if side == 'SHORT':
                                            new_trailing_sl = round(current_price * (1 + trailing_dist_pct / 100), 4)
                                            improved = new_trailing_sl < current_trailing_sl  # for SHORT, lower SL is better
                                        else:
                                            new_trailing_sl = round(current_price * (1 - trailing_dist_pct / 100), 4)
                                            improved = new_trailing_sl > current_trailing_sl  # for LONG, higher SL is better
                                        if improved:
                                            ts_result = ex.set_position_sl(
                                                symbol=symbol, stop_loss=str(new_trailing_sl), side=bybit_side,
                                                trailing_active=True, trailing_distance=str(trailing_dist_price),
                                            )
                                            if ts_result.get('retCode') == 0:
                                                db_pos.trailing_stop_price = Decimal(str(new_trailing_sl))
                                                sess.commit()
                                                self.log('info', f"Trailing MOVED for {side} {symbol}: ${current_trailing_sl:.4f} → ${new_trailing_sl:.4f}")
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
                 'stop_loss': s.stop_loss, 'take_profit': s.take_profit,
                 'exit_plan': dict(s.exit_plan_json) if s.exit_plan_json else None}
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

                # Scalp defenses (daily-loss / global cap / SL cooldown)
                allowed, reason = self._check_defenses_pre_open()
                if not allowed:
                    self.log('info', f"Skipping BUY {symbol}: {reason}")
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
                # H2: уважать position_usdt из сигнала, если задан (Clone5 пишет в exit_plan_json)
                signal_pos = None
                if sdata.get('exit_plan') and isinstance(sdata['exit_plan'], dict):
                    sp = sdata['exit_plan'].get('position_usdt')
                    if sp is not None:
                        try:
                            signal_pos = float(sp)
                        except (TypeError, ValueError):
                            signal_pos = None
                base_amount = signal_pos if signal_pos else self.position_usd
                amount = min(base_amount, usdt_balance)  # FIXED minimal margin (e.g. $5), NOT full balance

                if amount < 5:
                    self.log('warning', f"Balance too small: ${amount:.2f}")
                    self._update_strategy_signal_status(signal_id, 'skipped')
                    continue

                amount_str = str(round(amount, 2))

                # H4: уважать SL/TP из сигнала, если они есть; иначе ATR-адаптив
                signal_sl = sdata.get('stop_loss')
                signal_tp = sdata.get('take_profit')
                if signal_sl and signal_tp and signal_sl > 0 and signal_tp > 0:
                    self.log('info', f"BUY {symbol} using signal SL/TP (${signal_sl:.4f}/${signal_tp:.4f})")
                    result = self.execute_linear_buy(
                        symbol, amount_str, exchange,
                        stop_loss_price=str(signal_sl), take_profit_price=str(signal_tp),
                    )
                else:
                    sl_pct, tp_pct, atr_note = self._adaptive_sl_tp_pct(symbol, exchange)
                    self.log('info', f"BUY {symbol} sizing: {atr_note}")
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
                    side='LONG',
                    exit_plan=sdata.get('exit_plan'),
                    strategy=sdata.get('strategy'),  # M2: per-strategy dedup
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
        """Update StrategySignal status. Cascades to linked Signal:
        - 'executed'  → signal.status = 'CONSUMED'
        - 'skipped'/'failed' → signal.status = 'CANCELLED'
        - other       → signal unchanged
        """
        try:
            with self.db.get_session() as session:
                sig = session.query(StrategySignal).get(signal_id)
                if not sig:
                    return
                sig.status = status
                if status == 'executed':
                    sig.executed_at = datetime.utcnow()
                # Cascade to source signal
                if sig.source_signal_id is not None:
                    from src.core.database import Signal
                    src = session.query(Signal).get(sig.source_signal_id)
                    if src and src.status == 'PENDING':
                        if status == 'executed':
                            src.status = 'CONSUMED'
                        elif status in ('skipped', 'failed'):
                            src.status = 'CANCELLED'
                session.commit()
        except Exception as e:
            self.log('error', f"Failed update signal {signal_id}: {e}")

    def execute_linear_buy(self, symbol: str, amount: str, exchange: str,
                           sl_pct: Optional[float] = None, tp_pct: Optional[float] = None,
                           stop_loss_price: Optional[str] = None, take_profit_price: Optional[str] = None) -> Dict:
        """Open LONG position on Bybit linear via ccxt (handles signature correctly).

        H4: если переданы абсолютные stop_loss_price / take_profit_price, используем их
        вместо pct-расчёта (SL/TP из стратегии). Иначе считаем через sl_pct/tp_pct.
        """
        try:
            if exchange != 'bybit':
                ex = self.exchanges.get(exchange)
                if not ex:
                    return {'error': f'No exchange: {exchange}'}
                ticker = ex.get_ticker(symbol)
                current_price = float(ticker.get('lastPrice', 0))
            else:
                # Use ccxt for bybit - calculate qty from amount_usdt
                amount_usdt = float(amount)
                balance = self._get_bybit_balance_ccxt()
                if balance['free'] < amount_usdt:
                    return {'error': f'Insufficient balance: ${balance["free"]:.2f} < ${amount_usdt:.2f}'}

                ccxt_bybit = self._bybit()
                if not ccxt_bybit:
                    return {'error': 'ccxt bybit not initialized'}

                # Minimal sizing: target notional = margin($amount_usdt) * leverage, rounded to the
                # pair's real qtyStep, bumped up to satisfy minOrderQty AND minNotionalValue ($5).
                import math
                sym = symbol.upper().replace('USDT', '/USDT:USDT')
                ccxt_bybit.load_markets()
                m = ccxt_bybit.market(sym)
                price = ccxt_bybit.fetch_ticker(sym).get('last') or 1
                lsf = (m.get('info', {}) or {}).get('lotSizeFilter', {})
                step = float(lsf.get('qtyStep') or (m.get('precision', {}) or {}).get('amount') or 0.01)
                min_qty = float(lsf.get('minOrderQty') or step)
                min_notional = float(lsf.get('minNotionalValue') or 5.0)
                target_notional = amount_usdt * self.leverage
                qty = math.floor((target_notional / price) / step) * step
                guard = 0
                while (qty < min_qty or qty * price < min_notional) and guard < 1000:
                    qty = round(qty + step, 10); guard += 1
                qty = float(f"{qty:.10f}".rstrip('0').rstrip('.') or 0)
                if qty <= 0:
                    return {'error': f'Computed qty {qty} invalid for {symbol}'}
                # Set leverage explicitly — Bybit account leverage "sticks" between orders.
                try:
                    ccxt_bybit.set_leverage(self.leverage, sym)
                except Exception as le:
                    if '110043' not in str(le) and 'not modified' not in str(le).lower():
                        self.log('warning', f"set_leverage({sym},{self.leverage}x): {le}")
                self.log('info', f"BUY {symbol}: qty={qty} notional=${qty*price:.2f} lev={self.leverage}x")
                order = ccxt_bybit.create_order(sym, 'market', 'buy', qty)
                result = {
                    'retCode': 0,
                    'orderId': order.get('id'),
                    'executed_qty': float(order.get('filled', qty)),
                    'avgPrice': float(order.get('average', price)),
                }
                result['cummulative_quote_qty'] = result['executed_qty'] * result['avgPrice']
                return result
            current_price = float(ticker.get('lastPrice', 0))
            if current_price <= 0:
                return {'error': 'No price'}
            amount_usdt = float(amount)

            # Step 1: Choose leverage. Cap at 5× (cuts $5×5=$25 exposure; SL 1% = $0.25 loss).
            # Per-pair leverage on Bybit "sticks" between sessions — must set explicitly.
            min_lev_for_notional = max(5.0 / amount_usdt, 1)
            leverage = min(5, max(int(min_lev_for_notional), 1))
            try:
                ex.set_leverage(symbol, str(leverage), str(leverage))
            except Exception as lev_err:
                # Bybit returns "leverage not modified" if already set — ignore
                if '110043' not in str(lev_err):
                    self.log('warning', f"set_leverage({symbol},{leverage}x) failed: {lev_err}")

            # Step 2: Calculate qty with that leverage, round to lot size step.
            # M4 fix (08.06.2026): qtyStep из ccxt market() вместо хардкода 0.01
            step = self._get_market_qty_step(symbol) if hasattr(self, '_get_market_qty_step') else 0.01
            pos_value = amount_usdt * leverage
            qty = pos_value / current_price
            qty = round(qty / step) * step
            qty = float(f"{qty:.10f}".rstrip('0').rstrip('.') or 0)

            if qty <= 0:
                return {'error': f'Qty {qty:.4f} below min {step}'}

            # Step 2b: SL price (H4: приоритет абсолютному stop_loss_price из сигнала)
            if stop_loss_price:
                stop_loss = str(stop_loss_price)
            elif sl_pct is not None:
                stop_loss = str(round(current_price * (1 - sl_pct / 100), 4))
            else:
                stop_loss = str(round(current_price * (1 - self.default_sl_pct / 100), 4))

            # H4: take_profit_price из сигнала → ставим на Bybit (если передан)
            take_profit_arg = str(take_profit_price) if take_profit_price else None

            # Step 3: Open position WITHOUT take_profit by default — Bybit-side TP closes
            # FULL qty in one shot, vetoing our multi-TP partial closes.
            # Если передан take_profit_price из сигнала — уважаем его.
            create_kwargs = dict(
                symbol=symbol, side='Buy', order_type='Market', qty=str(qty),
                leverage=str(leverage),
                stop_loss=stop_loss,
            )
            if take_profit_arg:
                create_kwargs['take_profit'] = take_profit_arg
            result = ex.create_linear_order(**create_kwargs)

            if 'error' in result:
                return result

            # Step 4: Trailing-stop NOT set at open time — Bybit V5 trailingStop without
            # activePrice activates immediately and triggers on first-tick volatility.
            # Trailing is activated later in _check_and_execute_sl_tp once position
            # reaches trailing_stop_activation_percent profit.

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

    def execute_linear_short(self, symbol: str, amount: str, exchange: str,
                             sl_pct: Optional[float] = None, tp_pct: Optional[float] = None,
                             stop_loss_price: Optional[str] = None, take_profit_price: Optional[str] = None) -> Dict:
        """Open SHORT position on linear perpetuals. Mirrors execute_linear_buy.

        H4: если переданы абсолютные stop_loss_price / take_profit_price — уважаем их.
        """
        try:
            ex = self.exchanges.get(exchange)
            if not ex:
                return {'error': f'No exchange: {exchange}'}
            ticker = ex.get_ticker(symbol)
            current_price = float(ticker.get('lastPrice', 0))
            if current_price <= 0:
                return {'error': 'No price'}
            amount_usdt = float(amount)

            # Leverage cap 5× + explicit set_leverage (per-pair settings are sticky)
            min_lev_for_notional = max(5.0 / amount_usdt, 1)
            leverage = min(5, max(int(min_lev_for_notional), 1))
            try:
                ex.set_leverage(symbol, str(leverage), str(leverage))
            except Exception as lev_err:
                if '110043' not in str(lev_err):
                    self.log('warning', f"set_leverage({symbol},{leverage}x) failed: {lev_err}")

            pos_value = amount_usdt * leverage
            # M4 fix (08.06.2026): qtyStep из ccxt market() вместо хардкода 0.01.
            # Fallback на 0.01 для пар где ccxt не вернул lot info.
            step = 0.01
            try:
                ccxt_b = self._bybit()
                if ccxt_b:
                    sym_ccxt = symbol.upper().replace('USDT', '/USDT:USDT')
                    m = ccxt_b.market(sym_ccxt)
                    lsf = (m.get('info', {}) or {}).get('lotSizeFilter', {})
                    s = float(lsf.get('qtyStep') or (m.get('precision', {}) or {}).get('amount') or 0.01)
                    if s > 0:
                        step = s
            except Exception:
                pass
            qty = pos_value / current_price
            qty = round(qty / step) * step
            # Strip trailing zeros (напр. 0.10000 → 0.1) для совместимости с Bybit
            qty = float(f"{qty:.10f}".rstrip('0').rstrip('.') or 0)

            if qty <= 0:
                return {'error': f'Qty {qty:.4f} below min {step}'}

            # H4: SL price (приоритет абсолютному stop_loss_price из сигнала)
            if stop_loss_price:
                stop_loss = str(stop_loss_price)
            elif sl_pct is not None:
                stop_loss = str(round(current_price * (1 + sl_pct / 100), 4))
            else:
                stop_loss = str(round(current_price * (1 + self.default_sl_pct / 100), 4))

            # H4: take_profit_price из сигнала → ставим на Bybit (если передан)
            take_profit_arg = str(take_profit_price) if take_profit_price else None

            # SHORT: SL above entry. TP managed by our partial-close cycle, NOT Bybit-side
            # by default; уважаем абсолютный TP из сигнала, если передан.
            create_kwargs = dict(
                symbol=symbol, side='Sell', order_type='Market', qty=str(qty),
                leverage=str(leverage),
                stop_loss=stop_loss,
            )
            if take_profit_arg:
                create_kwargs['take_profit'] = take_profit_arg
            result = ex.create_linear_order(**create_kwargs)
            if 'error' in result:
                return result

            # Trailing-stop deferred — see note in execute_linear_buy for rationale.

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

            result['side'] = 'SHORT'
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
                {'id': s.id, 'symbol': s.symbol, 'exchange': s.exchange or 'bybit',
                 'strategy': s.strategy,  # M2: per-strategy dedup
                 'stop_loss': s.stop_loss, 'take_profit': s.take_profit,
                 'exit_plan': dict(s.exit_plan_json) if s.exit_plan_json else None}
                for s in pending_sells_raw
            ]

        for sdata in pending_sells:
            try:
                symbol = sdata['symbol']
                exchange = sdata['exchange']
                signal_id = sdata['id']

                # Find open position
                position = self.get_open_position(symbol, exchange)

                # No position → open SHORT (scalp on linear perpetuals)
                if not position:
                    allowed, reason = self._check_defenses_pre_open()
                    if not allowed:
                        self.log('info', f"Skipping SHORT-open {symbol}: {reason}")
                        self._update_strategy_signal_status(signal_id, 'skipped')
                        continue

                    usdt_balance = self.get_usdt_balance(exchange)
                    if usdt_balance < 5.0:
                        self.log('info', f"Skipping SHORT {symbol}: balance ${usdt_balance:.2f} < $5")
                        self._update_strategy_signal_status(signal_id, 'skipped')
                        continue

                    # H2: уважать position_usdt из сигнала
                    signal_pos = None
                    if sdata.get('exit_plan') and isinstance(sdata['exit_plan'], dict):
                        sp = sdata['exit_plan'].get('position_usdt')
                        if sp is not None:
                            try:
                                signal_pos = float(sp)
                            except (TypeError, ValueError):
                                signal_pos = None
                    short_amount = signal_pos if signal_pos else min(self.position_usd, usdt_balance)
                    self.log('info', f"SHORT {symbol} pos=${short_amount:.2f}")

                    # H4: уважать SL/TP из сигнала
                    signal_sl = sdata.get('stop_loss')
                    signal_tp = sdata.get('take_profit')
                    if signal_sl and signal_tp and signal_sl > 0 and signal_tp > 0:
                        self.log('info', f"SHORT {symbol} using signal SL/TP (${signal_sl:.4f}/${signal_tp:.4f})")
                        short_result = self.execute_linear_short(
                            symbol, str(short_amount), exchange,
                            stop_loss_price=str(signal_sl), take_profit_price=str(signal_tp),
                        )
                    else:
                        sl_pct_s, tp_pct_s, atr_note_s = self._adaptive_sl_tp_pct(symbol, exchange)
                        self.log('info', f"SHORT {symbol} sizing: {atr_note_s}")
                        short_result = self.execute_linear_short(symbol, str(short_amount), exchange, sl_pct_s, tp_pct_s)
                    if 'error' in short_result:
                        errors += 1
                        self.log('error', f"SHORT open failed for {symbol}: {short_result['error']}")
                        self._update_strategy_signal_status(signal_id, 'failed')
                        continue

                    entry_price = short_result.get('avgPrice', 0) or 0
                    qty_filled = short_result.get('executed_qty', 0) or 0
                    cost = qty_filled * entry_price

                    position_id = self.create_position(
                        symbol=symbol, exchange=exchange,
                        entry_price=entry_price, quantity=qty_filled,
                        cost_usdt=cost, signal_id=signal_id,
                        market_type='linear', side='SHORT',
                        exit_plan=sdata.get('exit_plan'),
                        strategy=sdata.get('strategy'),  # M2: per-strategy dedup
                    )

                    self.save_trade_to_db(signal_id, {**short_result, 'price': entry_price}, position_id=position_id)
                    self._update_strategy_signal_status(signal_id, 'executed')
                    executed += 1
                    details.append({
                        'type': 'SHORT_OPEN', 'symbol': symbol,
                        'price': entry_price, 'quantity': qty_filled,
                    })
                    continue

                # Position exists → close it (works for LONG via execute_linear_sell)
                ex = self.exchanges.get(exchange)
                ticker = ex.get_ticker(symbol)
                current_price = float(ticker.get('lastPrice', 0))

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
