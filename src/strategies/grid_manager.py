"""
Grid Trading Module for CryptoTrader
Place, manage, and monitor grid orders on Bybit (spot + linear).
Budget-aware, max 3 levels, ATR-based step sizing.
"""

import json
import time
import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

import psycopg2

logger = logging.getLogger(__name__)


@dataclass
class GridLevel:
    step: int
    side: str          # 'Buy' or 'Sell'
    price: float
    amount: float      # in base coin
    amount_usdt: float
    order_id: str = ''
    status: str = 'pending'  # pending, placed, filled, cancelled
    filled_at: Optional[datetime] = None


@dataclass
class GridConfig:
    grid_id: str
    symbol: str
    market_type: str   # 'spot' or 'linear'
    direction: str     # 'long' (buy dip) or 'short' (sell rally) or 'neutral' (both sides)
    levels: List[GridLevel]
    tp_price: float
    sl_price: float
    total_budget: float
    created_at: datetime
    status: str = 'active'  # active, completed, cancelled, stopped_out


class GridManager:
    """Manage grid orders lifecycle"""

    MAX_LEVELS = 3
    MAX_BUDGET_PCT = 0.30  # 30% of free balance
    MIN_STEP_PCT = 1.0
    MAX_STEP_PCT = 5.0

    def __init__(self, bybit_api, db_conn_params: dict):
        self.api = bybit_api
        self.db = db_conn_params

    def _get_conn(self):
        return psycopg2.connect(**self.db)

    def _ensure_table(self):
        """Create grid tables if not exist"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS grids (
                    grid_id VARCHAR(36) PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    market_type VARCHAR(10) NOT NULL,
                    direction VARCHAR(10) NOT NULL,
                    config_json JSONB NOT NULL,
                    tp_price NUMERIC,
                    sl_price NUMERIC,
                    total_budget NUMERIC,
                    status VARCHAR(10) DEFAULT 'active',
                    realized_pnl NUMERIC DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    closed_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS grid_orders (
                    id SERIAL PRIMARY KEY,
                    grid_id VARCHAR(36) REFERENCES grids(grid_id),
                    step INT NOT NULL,
                    side VARCHAR(10) NOT NULL,
                    price NUMERIC NOT NULL,
                    amount NUMERIC NOT NULL,
                    amount_usdt NUMERIC NOT NULL,
                    order_id VARCHAR(36) DEFAULT '',
                    status VARCHAR(10) DEFAULT 'pending',
                    filled_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def calculate_grid(self, symbol: str, current_price: float,
                       direction: str, budget_usdt: float,
                       step_pct: float = 2.5,
                       market_type: str = 'spot') -> GridConfig:
        """Calculate grid levels based on current price and direction"""
        grid_id = str(uuid.uuid4())[:8]
        levels = []

        # Budget split: 22% / 33% / 45% (conservative martingale-like)
        splits = [0.22, 0.33, 0.45]

        for i in range(self.MAX_LEVELS):
            if direction == 'long':
                # Buy lower on dips
                price = round(current_price * (1 - step_pct / 100 * (i + 1)), 6)
                side = 'Buy'
            elif direction == 'short':
                # Sell higher on rallies
                price = round(current_price * (1 + step_pct / 100 * (i + 1)), 6)
                side = 'Sell'
            else:
                # Neutral: alternate buy/sell
                if i % 2 == 0:
                    price = round(current_price * (1 - step_pct / 100 * (i + 1)), 6)
                    side = 'Buy'
                else:
                    price = round(current_price * (1 + step_pct / 100 * (i + 1)), 6)
                    side = 'Sell'

            amount_usdt = round(budget_usdt * splits[i], 2)
            amount = round(amount_usdt / price, 8)

            levels.append(GridLevel(
                step=i + 1,
                side=side,
                price=price,
                amount=amount,
                amount_usdt=amount_usdt,
            ))

        # TP/SL calculation
        if direction == 'long':
            tp_price = round(current_price * 1.02, 6)   # +2% from current
            sl_price = round(current_price * (1 - step_pct / 100 * (self.MAX_LEVELS + 1)), 6)
        elif direction == 'short':
            tp_price = round(current_price * 0.98, 6)
            sl_price = round(current_price * (1 + step_pct / 100 * (self.MAX_LEVELS + 1)), 6)
        else:
            tp_price = round(current_price * 1.02, 6)
            sl_price = round(current_price * 0.96, 6)

        return GridConfig(
            grid_id=grid_id,
            symbol=symbol,
            market_type=market_type,
            direction=direction,
            levels=levels,
            tp_price=tp_price,
            sl_price=sl_price,
            total_budget=budget_usdt,
            created_at=datetime.now(timezone.utc),
        )

    def place_grid(self, grid: GridConfig) -> Dict:
        """Place all grid orders on exchange"""
        self._ensure_table()
        results = []
        category = grid.market_type

        for level in grid.levels:
            try:
                # Round qty according to lot size
                qty_str = self.api.round_qty_by_lot_size(
                    grid.symbol, level.amount, category=category
                )

                if category == 'spot' and level.side == 'Buy':
                    # For spot buy, qty is in quoteCoin (USDT)
                    qty_str = f"{level.amount_usdt:.2f}"
                    resp = self.api.create_order(
                        category='spot', symbol=grid.symbol, side='Buy',
                        order_type='Limit', qty=qty_str,
                        price=f"{level.price:.6f}",
                        market_unit='quoteCoin',
                        order_link_id=f"grid_{grid.grid_id}_{level.step}"
                    )
                elif category == 'spot' and level.side == 'Sell':
                    resp = self.api.create_order(
                        category='spot', symbol=grid.symbol, side='Sell',
                        order_type='Limit', qty=qty_str,
                        price=f"{level.price:.6f}",
                        order_link_id=f"grid_{grid.grid_id}_{level.step}"
                    )
                elif category == 'linear' and level.side == 'Buy':
                    resp = self.api.create_linear_long(
                        grid.symbol, qty=qty_str, leverage='3',
                        order_type='Limit', price=f"{level.price:.6f}",
                        order_link_id=f"grid_{grid.grid_id}_{level.step}"
                    )
                elif category == 'linear' and level.side == 'Sell':
                    resp = self.api.create_linear_short(
                        grid.symbol, qty=qty_str, leverage='3',
                        order_type='Limit', price=f"{level.price:.6f}",
                        order_link_id=f"grid_{grid.grid_id}_{level.step}"
                    )
                else:
                    continue

                order_id = resp.get('result', {}).get('orderId', '')
                level.order_id = order_id
                level.status = 'placed'
                results.append({'step': level.step, 'order_id': order_id, 'status': 'placed'})
                logger.info(f"Grid {grid.grid_id} step {level.step}: {level.side} {qty_str} @ {level.price}")

            except Exception as e:
                level.status = 'failed'
                results.append({'step': level.step, 'status': 'failed', 'error': str(e)})
                logger.error(f"Grid {grid.grid_id} step {level.step} failed: {e}")

        # Save to DB
        self._save_grid(grid)

        return {'grid_id': grid.grid_id, 'orders': results}

    def _save_grid(self, grid: GridConfig):
        """Persist grid config and orders to DB"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO grids (grid_id, symbol, market_type, direction, config_json,
                                   tp_price, sl_price, total_budget, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                grid.grid_id, grid.symbol, grid.market_type, grid.direction,
                json.dumps([asdict(l) for l in grid.levels]),
                grid.tp_price, grid.sl_price, grid.total_budget, grid.status
            ))

            for level in grid.levels:
                cur.execute("""
                    INSERT INTO grid_orders (grid_id, step, side, price, amount, amount_usdt, order_id, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    grid.grid_id, level.step, level.side, level.price,
                    level.amount, level.amount_usdt, level.order_id, level.status
                ))
            conn.commit()
        finally:
            conn.close()

    def check_grid_status(self, grid_id: str) -> Dict:
        """Check current state of a grid: filled orders, P&L estimate"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # Get grid config
            cur.execute("SELECT symbol, market_type, direction, tp_price, sl_price, status FROM grids WHERE grid_id = %s", (grid_id,))
            row = cur.fetchone()
            if not row:
                return {'error': f'Grid {grid_id} not found'}

            symbol, market_type, direction, tp_price, sl_price, grid_status = row

            # Get orders
            cur.execute("""
                SELECT step, side, price, amount, amount_usdt, order_id, status, filled_at
                FROM grid_orders WHERE grid_id = %s ORDER BY step
            """, (grid_id,))
            orders = cur.fetchall()

            # Check exchange for fills
            filled_count = 0
            total_cost = 0
            total_qty = 0
            for order in orders:
                step, side, price, amount, amount_usdt, order_id, status, filled_at = order
                if order_id and status == 'placed':
                    try:
                        # Check order status on exchange
                        exchange_orders = self.api.get_open_orders(
                            category=market_type, symbol=symbol
                        )
                        is_still_open = False
                        for ex_order in exchange_orders.get('result', {}).get('list', []):
                            if ex_order.get('orderId') == order_id:
                                is_still_open = True
                                break

                        if not is_still_open:
                            # Order was filled or cancelled - check history
                            history = self.api.get_order_history(
                                category=market_type, symbol=symbol, limit=20
                            )
                            for hist in history.get('result', {}).get('list', []):
                                if hist.get('orderId') == order_id:
                                    if hist.get('orderStatus') == 'Filled':
                                        cur.execute("""
                                            UPDATE grid_orders SET status='filled', filled_at=NOW()
                                            WHERE grid_id=%s AND step=%s
                                        """, (grid_id, step))
                                        filled_count += 1
                                        total_cost += amount_usdt
                                        total_qty += amount
                                    elif hist.get('orderStatus') == 'Cancelled':
                                        cur.execute("""
                                            UPDATE grid_orders SET status='cancelled'
                                            WHERE grid_id=%s AND step=%s
                                        """, (grid_id, step))
                                    break
                    except Exception as e:
                        logger.warning(f"Error checking order {order_id}: {e}")

                elif status == 'filled':
                    filled_count += 1
                    total_cost += float(amount_usdt or 0)
                    total_qty += float(amount or 0)

            conn.commit()

            # Calculate avg entry and P&L
            avg_entry = total_cost / total_qty if total_qty > 0 else 0
            ticker = self.api.get_ticker(symbol)
            current_price = float(ticker.get('lastPrice', 0))

            if filled_count > 0 and total_qty > 0:
                if direction == 'long':
                    unrealized_pnl = (current_price - avg_entry) * total_qty
                else:
                    unrealized_pnl = (avg_entry - current_price) * total_qty
            else:
                unrealized_pnl = 0

            return {
                'grid_id': grid_id,
                'symbol': symbol,
                'direction': direction,
                'status': grid_status,
                'total_orders': len(orders),
                'filled': filled_count,
                'pending': len(orders) - filled_count,
                'total_cost_usdt': round(total_cost, 4),
                'total_qty': round(total_qty, 8),
                'avg_entry': round(avg_entry, 6),
                'current_price': current_price,
                'unrealized_pnl': round(unrealized_pnl, 4),
                'tp_price': float(tp_price) if tp_price else None,
                'sl_price': float(sl_price) if sl_price else None,
            }
        finally:
            conn.close()

    def cancel_grid(self, grid_id: str) -> Dict:
        """Cancel all unfilled orders in a grid"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT symbol, market_type FROM grids WHERE grid_id = %s", (grid_id,))
            row = cur.fetchone()
            if not row:
                return {'error': f'Grid {grid_id} not found'}

            symbol, market_type = row

            # Get pending orders
            cur.execute("""
                SELECT step, order_id FROM grid_orders
                WHERE grid_id = %s AND status = 'placed'
            """, (grid_id,))
            pending = cur.fetchall()

            cancelled = 0
            for step, order_id in pending:
                if order_id:
                    try:
                        self.api.cancel_order(market_type, symbol, order_id=order_id)
                        cur.execute("""
                            UPDATE grid_orders SET status='cancelled'
                            WHERE grid_id=%s AND step=%s
                        """, (grid_id, step))
                        cancelled += 1
                    except Exception as e:
                        logger.warning(f"Cancel order {order_id} failed: {e}")

            cur.execute("""
                UPDATE grids SET status='cancelled', updated_at=NOW() WHERE grid_id=%s
            """, (grid_id,))
            conn.commit()

            return {'grid_id': grid_id, 'cancelled_orders': cancelled}
        finally:
            conn.close()

    def close_grid_position(self, grid_id: str) -> Dict:
        """Close filled grid position with market order"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT symbol, market_type, direction FROM grids WHERE grid_id = %s", (grid_id,))
            row = cur.fetchone()
            if not row:
                return {'error': f'Grid {grid_id} not found'}

            symbol, market_type, direction = row

            # Get filled orders
            cur.execute("""
                SELECT SUM(amount) FROM grid_orders WHERE grid_id = %s AND status = 'filled'
            """, (grid_id,))
            total_qty = float(cur.fetchone()[0] or 0)

            if total_qty == 0:
                return {'error': 'No filled orders to close'}

            # Close with market order
            close_side = 'Sell' if direction == 'long' else 'Buy'
            qty_str = self.api.round_qty_by_lot_size(symbol, total_qty, category=market_type)

            if market_type == 'spot':
                if close_side == 'Sell':
                    resp = self.api.create_spot_sell(symbol, qty_str)
                else:
                    resp = self.api.create_spot_buy(symbol, qty_str)
            else:
                if close_side == 'Sell':
                    resp = self.api.create_linear_short(symbol, qty_str, reduce_only=True)
                else:
                    resp = self.api.create_linear_long(symbol, qty_str, reduce_only=True)

            # Cancel remaining unfilled
            self.cancel_grid(grid_id)

            # Mark grid completed
            cur.execute("""
                UPDATE grids SET status='completed', closed_at=NOW(), updated_at=NOW()
                WHERE grid_id=%s
            """, (grid_id,))
            conn.commit()

            return {'grid_id': grid_id, 'closed_qty': qty_str, 'close_side': close_side}
        finally:
            conn.close()

    def list_active_grids(self) -> List[Dict]:
        """List all active grids"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT grid_id, symbol, market_type, direction, total_budget, status, created_at
                FROM grids WHERE status = 'active'
                ORDER BY created_at DESC
            """)
            rows = cur.fetchall()
            return [
                {
                    'grid_id': r[0], 'symbol': r[1], 'market_type': r[2],
                    'direction': r[3], 'budget': float(r[4]), 'status': r[5],
                    'created': str(r[6])
                }
                for r in rows
            ]
        finally:
            conn.close()
