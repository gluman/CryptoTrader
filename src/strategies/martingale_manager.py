"""
Martingale Module for CryptoTrader
Soft 3-step martingale: x1.0 → x1.5 → x2.0
Max risk per chain: 2% of balance
Cooldown after stop-out: 24h
"""

import json
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

import psycopg2

logger = logging.getLogger(__name__)


@dataclass
class MartingaleStep:
    step: int           # 1, 2, or 3
    side: str           # 'Buy' or 'Sell'
    price: float
    amount: float       # base coin qty
    amount_usdt: float
    multiplier: float   # 1.0, 1.5, 2.0
    order_id: str = ''
    status: str = 'pending'  # pending, placed, filled, failed
    filled_at: Optional[datetime] = None


@dataclass
class MartingaleChain:
    chain_id: str
    symbol: str
    market_type: str    # 'spot' or 'linear'
    direction: str      # 'long' or 'short'
    steps: List[MartingaleStep]
    step_pct: float     # % between steps (e.g. 2.5)
    avg_entry: float
    breakeven_price: float
    tp_price: float
    sl_price: float
    total_budget: float
    status: str = 'active'  # active, completed, stopped_out, cooldown
    created_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


class MartingaleManager:
    """Manage martingale chains with 3-step soft progression"""

    MAX_STEPS = 3
    MULTIPLIERS = [1.0, 1.5, 2.0]       # Soft progression (NOT 1,2,4)
    STEP_SPLITS = [0.22, 0.33, 0.45]    # Budget allocation per step
    COOLDOWN_HOURS = 24
    MAX_RISK_PCT = 0.02                  # 2% of balance per chain

    def __init__(self, bybit_api, db_conn_params: dict):
        self.api = bybit_api
        self.db = db_conn_params

    def _get_conn(self):
        return psycopg2.connect(**self.db)

    def _ensure_table(self):
        """Create martingale tables if not exist"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS martingale_chains (
                    chain_id VARCHAR(36) PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    market_type VARCHAR(10) NOT NULL,
                    direction VARCHAR(10) NOT NULL,
                    step_pct NUMERIC NOT NULL,
                    avg_entry NUMERIC,
                    breakeven_price NUMERIC,
                    tp_price NUMERIC,
                    sl_price NUMERIC,
                    total_budget NUMERIC,
                    status VARCHAR(15) DEFAULT 'active',
                    realized_pnl NUMERIC DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    closed_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS martingale_steps (
                    id SERIAL PRIMARY KEY,
                    chain_id VARCHAR(36) REFERENCES martingale_chains(chain_id),
                    step INT NOT NULL,
                    side VARCHAR(10) NOT NULL,
                    price NUMERIC NOT NULL,
                    amount NUMERIC NOT NULL,
                    amount_usdt NUMERIC NOT NULL,
                    multiplier NUMERIC NOT NULL,
                    order_id VARCHAR(36) DEFAULT '',
                    status VARCHAR(10) DEFAULT 'pending',
                    filled_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS martingale_cooldowns (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    chain_id VARCHAR(36),
                    cooldown_until TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def check_cooldown(self, symbol: str) -> Optional[datetime]:
        """Check if symbol is in cooldown. Returns cooldown end time or None."""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT cooldown_until FROM martingale_cooldowns
                WHERE symbol = %s AND cooldown_until > NOW()
                ORDER BY cooldown_until DESC LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def calculate_chain(self, symbol: str, current_price: float,
                        direction: str, budget_usdt: float,
                        step_pct: float = 2.5,
                        market_type: str = 'spot') -> MartingaleChain:
        """Calculate martingale chain parameters"""
        chain_id = str(uuid.uuid4())[:8]
        steps = []

        total_qty = 0
        total_cost = 0

        for i in range(self.MAX_STEPS):
            if direction == 'long':
                price = round(current_price * (1 - step_pct / 100 * i), 6)
                side = 'Buy'
            else:
                price = round(current_price * (1 + step_pct / 100 * i), 6)
                side = 'Sell'

            amount_usdt = round(budget_usdt * self.STEP_SPLITS[i], 2)
            amount = round(amount_usdt / price, 8)
            total_qty += amount
            total_cost += amount_usdt

            steps.append(MartingaleStep(
                step=i + 1,
                side=side,
                price=price,
                amount=amount,
                amount_usdt=amount_usdt,
                multiplier=self.MULTIPLIERS[i],
            ))

        avg_entry = total_cost / total_qty if total_qty > 0 else current_price

        if direction == 'long':
            tp_price = round(current_price, 6)           # TP at original price = profit
            sl_price = round(current_price * (1 - step_pct / 100 * (self.MAX_STEPS + 1)), 6)
        else:
            tp_price = round(current_price, 6)
            sl_price = round(current_price * (1 + step_pct / 100 * (self.MAX_STEPS + 1)), 6)

        breakeven_price = round(avg_entry, 6)

        return MartingaleChain(
            chain_id=chain_id,
            symbol=symbol,
            market_type=market_type,
            direction=direction,
            steps=steps,
            step_pct=step_pct,
            avg_entry=avg_entry,
            breakeven_price=breakeven_price,
            tp_price=tp_price,
            sl_price=sl_price,
            total_budget=total_cost,
            created_at=datetime.now(timezone.utc),
        )

    def place_chain(self, chain: MartingaleChain, start_step: int = 1) -> Dict:
        """Place martingale orders starting from given step"""
        self._ensure_table()

        # Check cooldown
        cooldown = self.check_cooldown(chain.symbol)
        if cooldown:
            return {'error': f'{chain.symbol} in cooldown until {cooldown}'}

        results = []
        category = chain.market_type

        for step in chain.steps:
            if step.step < start_step:
                continue  # Skip already filled steps

            try:
                qty_str = self.api.round_qty_by_lot_size(
                    chain.symbol, step.amount, category=category
                )

                if category == 'spot':
                    if step.side == 'Buy':
                        qty_str = f"{step.amount_usdt:.2f}"
                        resp = self.api.create_order(
                            category='spot', symbol=chain.symbol, side='Buy',
                            order_type='Limit', qty=qty_str,
                            price=f"{step.price:.6f}",
                            market_unit='quoteCoin',
                            order_link_id=f"mart_{chain.chain_id}_{step.step}"
                        )
                    else:
                        resp = self.api.create_order(
                            category='spot', symbol=chain.symbol, side='Sell',
                            order_type='Limit', qty=qty_str,
                            price=f"{step.price:.6f}",
                            order_link_id=f"mart_{chain.chain_id}_{step.step}"
                        )
                elif category == 'linear':
                    if step.side == 'Buy':
                        resp = self.api.create_linear_long(
                            chain.symbol, qty=qty_str, leverage='3',
                            order_type='Limit', price=f"{step.price:.6f}",
                        )
                    else:
                        resp = self.api.create_linear_short(
                            chain.symbol, qty=qty_str, leverage='3',
                            order_type='Limit', price=f"{step.price:.6f}",
                        )
                else:
                    continue

                order_id = resp.get('result', {}).get('orderId', '')
                step.order_id = order_id
                step.status = 'placed'
                results.append({
                    'step': step.step, 'order_id': order_id,
                    'price': step.price, 'qty': step.amount,
                    'status': 'placed'
                })
                logger.info(f"Martingale {chain.chain_id} step {step.step}: {step.side} @ {step.price}")

            except Exception as e:
                step.status = 'failed'
                results.append({'step': step.step, 'status': 'failed', 'error': str(e)})
                logger.error(f"Martingale step {step.step} failed: {e}")

        # Save to DB
        self._save_chain(chain)

        return {'chain_id': chain.chain_id, 'orders': results}

    def _save_chain(self, chain: MartingaleChain):
        """Persist chain to DB"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO martingale_chains
                (chain_id, symbol, market_type, direction, step_pct,
                 avg_entry, breakeven_price, tp_price, sl_price, total_budget, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                chain.chain_id, chain.symbol, chain.market_type, chain.direction,
                chain.step_pct, chain.avg_entry, chain.breakeven_price,
                chain.tp_price, chain.sl_price, chain.total_budget, chain.status
            ))

            for step in chain.steps:
                cur.execute("""
                    INSERT INTO martingale_steps
                    (chain_id, step, side, price, amount, amount_usdt, multiplier, order_id, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    chain.chain_id, step.step, step.side, step.price,
                    step.amount, step.amount_usdt, step.multiplier,
                    step.order_id, step.status
                ))
            conn.commit()
        finally:
            conn.close()

    def add_next_step(self, chain_id: str) -> Dict:
        """Manually trigger next step in chain (e.g. step 1 filled, place step 2)"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT symbol, market_type, direction FROM martingale_chains
                WHERE chain_id = %s AND status = 'active'
            """, (chain_id,))
            row = cur.fetchone()
            if not row:
                return {'error': f'Chain {chain_id} not active'}

            symbol, market_type, direction = row

            # Find last filled step
            cur.execute("""
                SELECT MAX(step) FROM martingale_steps
                WHERE chain_id = %s AND status = 'filled'
            """, (chain_id,))
            last_filled = cur.fetchone()[0] or 0

            next_step = last_filled + 1
            if next_step > self.MAX_STEPS:
                return {'error': f'All {self.MAX_STEPS} steps already filled'}

            # Get next step params
            cur.execute("""
                SELECT step, side, price, amount, amount_usdt
                FROM martingale_steps
                WHERE chain_id = %s AND step = %s
            """, (chain_id, next_step))
            step_row = cur.fetchone()
            if not step_row:
                return {'error': f'Step {next_step} not found'}

            step_num, side, price, amount, amount_usdt = step_row

            # Place order
            qty_str = self.api.round_qty_by_lot_size(symbol, float(amount), category=market_type)
            try:
                if market_type == 'spot' and side == 'Buy':
                    qty_str = f"{float(amount_usdt):.2f}"
                    resp = self.api.create_order(
                        category='spot', symbol=symbol, side='Buy',
                        order_type='Limit', qty=qty_str,
                        price=f"{float(price):.6f}", market_unit='quoteCoin'
                    )
                elif market_type == 'spot' and side == 'Sell':
                    resp = self.api.create_order(
                        category='spot', symbol=symbol, side='Sell',
                        order_type='Limit', qty=qty_str, price=f"{float(price):.6f}"
                    )
                elif market_type == 'linear' and side == 'Buy':
                    resp = self.api.create_linear_long(symbol, qty_str, leverage='3', order_type='Limit', price=f"{float(price):.6f}")
                else:
                    resp = self.api.create_linear_short(symbol, qty_str, leverage='3', order_type='Limit', price=f"{float(price):.6f}")

                order_id = resp.get('result', {}).get('orderId', '')
                cur.execute("""
                    UPDATE martingale_steps SET order_id=%s, status='placed'
                    WHERE chain_id=%s AND step=%s
                """, (order_id, chain_id, next_step))
                conn.commit()
                return {'chain_id': chain_id, 'step': next_step, 'order_id': order_id, 'status': 'placed'}

            except Exception as e:
                cur.execute("""
                    UPDATE martingale_steps SET status='failed'
                    WHERE chain_id=%s AND step=%s
                """, (chain_id, next_step))
                conn.commit()
                return {'error': str(e)}
        finally:
            conn.close()

    def check_chain_status(self, chain_id: str) -> Dict:
        """Check chain status, filled steps, P&L"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT symbol, market_type, direction, avg_entry, tp_price, sl_price, status
                FROM martingale_chains WHERE chain_id = %s
            """, (chain_id,))
            row = cur.fetchone()
            if not row:
                return {'error': f'Chain {chain_id} not found'}

            symbol, market_type, direction, avg_entry, tp_price, sl_price, status = row

            cur.execute("""
                SELECT step, side, price, amount, amount_usdt, status
                FROM martingale_steps WHERE chain_id = %s ORDER BY step
            """, (chain_id,))
            steps = cur.fetchall()

            filled_steps = [s for s in steps if s[5] == 'filled']
            total_qty = sum(float(s[3]) for s in filled_steps)
            total_cost = sum(float(s[4]) for s in filled_steps)

            ticker = self.api.get_ticker(symbol)
            current_price = float(ticker.get('lastPrice', 0))

            if total_qty > 0:
                real_avg = total_cost / total_qty
                if direction == 'long':
                    unrealized = (current_price - real_avg) * total_qty
                else:
                    unrealized = (real_avg - current_price) * total_qty
            else:
                real_avg = 0
                unrealized = 0

            return {
                'chain_id': chain_id,
                'symbol': symbol,
                'direction': direction,
                'status': status,
                'steps_total': len(steps),
                'steps_filled': len(filled_steps),
                'total_qty': round(total_qty, 8),
                'total_cost_usdt': round(total_cost, 4),
                'avg_entry': round(float(real_avg), 6),
                'breakeven': round(float(real_avg), 6),
                'current_price': current_price,
                'unrealized_pnl': round(unrealized, 4),
                'tp_price': float(tp_price) if tp_price else None,
                'sl_price': float(sl_price) if sl_price else None,
            }
        finally:
            conn.close()

    def close_chain(self, chain_id: str) -> Dict:
        """Close chain position and cancel pending orders"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT symbol, market_type, direction FROM martingale_chains
                WHERE chain_id = %s AND status = 'active'
            """, (chain_id,))
            row = cur.fetchone()
            if not row:
                return {'error': f'Chain {chain_id} not active'}

            symbol, market_type, direction = row

            # Cancel all pending limit orders for this chain
            cur.execute("""
                SELECT step, order_id FROM martingale_steps
                WHERE chain_id = %s AND status = 'placed'
            """, (chain_id,))
            pending = cur.fetchall()

            cancelled = 0
            for step, order_id in pending:
                if order_id:
                    try:
                        self.api.cancel_order(market_type, symbol, order_id=order_id)
                        cur.execute("""
                            UPDATE martingale_steps SET status='cancelled'
                            WHERE chain_id=%s AND step=%s
                        """, (chain_id, step))
                        cancelled += 1
                    except Exception as e:
                        logger.warning(f"Cancel step {step} failed: {e}")

            # Close filled position with market order
            cur.execute("""
                SELECT SUM(amount) FROM martingale_steps
                WHERE chain_id = %s AND status = 'filled'
            """, (chain_id,))
            total_qty = float(cur.fetchone()[0] or 0)

            close_result = None
            if total_qty > 0:
                close_side = 'Sell' if direction == 'long' else 'Buy'
                qty_str = self.api.round_qty_by_lot_size(symbol, total_qty, category=market_type)

                try:
                    if market_type == 'spot':
                        if close_side == 'Sell':
                            close_result = self.api.create_spot_sell(symbol, qty_str)
                        else:
                            close_result = self.api.create_spot_buy(symbol, qty_str)
                    else:
                        if close_side == 'Sell':
                            close_result = self.api.create_linear_short(symbol, qty_str, reduce_only=True)
                        else:
                            close_result = self.api.create_linear_long(symbol, qty_str, reduce_only=True)
                except Exception as e:
                    logger.error(f"Close chain market order failed: {e}")

            cur.execute("""
                UPDATE martingale_chains SET status='completed', closed_at=NOW(), updated_at=NOW()
                WHERE chain_id=%s
            """, (chain_id,))
            conn.commit()

            return {
                'chain_id': chain_id,
                'cancelled_orders': cancelled,
                'closed_qty': total_qty,
                'close_result': close_result
            }
        finally:
            conn.close()

    def trigger_stopout(self, chain_id: str) -> Dict:
        """Emergency close + cooldown"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT symbol FROM martingale_chains WHERE chain_id = %s", (chain_id,))
            row = cur.fetchone()
            if not row:
                return {'error': f'Chain {chain_id} not found'}

            symbol = row[0]

            # Close chain
            close_result = self.close_chain(chain_id)

            # Set cooldown
            cooldown_until = datetime.now(timezone.utc) + timedelta(hours=self.COOLDOWN_HOURS)
            cur.execute("""
                INSERT INTO martingale_cooldowns (symbol, chain_id, cooldown_until)
                VALUES (%s, %s, %s)
            """, (symbol, chain_id, cooldown_until))

            cur.execute("""
                UPDATE martingale_chains SET status='stopped_out' WHERE chain_id=%s
            """, (chain_id,))
            conn.commit()

            return {
                'chain_id': chain_id,
                'action': 'stopped_out',
                'cooldown_until': str(cooldown_until),
                'close_result': close_result,
            }
        finally:
            conn.close()

    def list_active_chains(self) -> List[Dict]:
        """List all active martingale chains"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT chain_id, symbol, market_type, direction, step_pct,
                       total_budget, status, created_at
                FROM martingale_chains WHERE status = 'active'
                ORDER BY created_at DESC
            """)
            rows = cur.fetchall()
            return [
                {
                    'chain_id': r[0], 'symbol': r[1], 'market_type': r[2],
                    'direction': r[3], 'step_pct': float(r[4]),
                    'budget': float(r[5]), 'status': r[6], 'created': str(r[7])
                }
                for r in rows
            ]
        finally:
            conn.close()
