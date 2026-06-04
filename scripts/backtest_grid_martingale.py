"""
Backtest Engine for Grid + Martingale strategies
Replays historical OHLCV data and simulates strategy execution.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import psycopg2

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    step: int
    side: str
    entry_price: float
    exit_price: float
    amount: float
    pnl_usdt: float
    pnl_pct: float
    hold_bars: int
    exit_reason: str  # 'tp', 'sl', 'timeout', 'grid_complete'


@dataclass
class BacktestResult:
    strategy: str           # 'grid', 'martingale', 'pair_trade'
    symbol: str
    start_date: str
    end_date: str
    initial_budget: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    trades: List[BacktestTrade] = field(default_factory=list)


class BacktestEngine:
    """Simulate grid and martingale strategies on historical data"""

    def __init__(self, db_conn_params: dict):
        self.db = db_conn_params

    def _get_conn(self):
        return psycopg2.connect(**self.db)

    def fetch_candles(self, symbol: str, timeframe: str = '60',
                      days: int = 30) -> List[Dict]:
        """Fetch historical candles from ohlcv_raw"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            since = datetime.now(timezone.utc) - timedelta(days=days)
            cur.execute("""
                SELECT timestamp, open, high, low, close, volume
                FROM ohlcv_raw
                WHERE symbol = %s AND timeframe = %s
                  AND timestamp >= %s
                ORDER BY timestamp ASC
            """, (symbol, timeframe, since))
            rows = cur.fetchall()
            return [
                {
                    'timestamp': r[0], 'open': float(r[1]), 'high': float(r[2]),
                    'low': float(r[3]), 'close': float(r[4]), 'volume': float(r[5])
                }
                for r in rows
            ]
        finally:
            conn.close()

    def backtest_martingale(self, symbol: str, budget: float = 5.0,
                            step_pct: float = 2.5, direction: str = 'long',
                            tp_pct: float = 3.0, sl_pct: float = 10.0,
                            timeframe: str = '60', days: int = 30) -> BacktestResult:
        """
        Backtest soft martingale (3 steps) on historical data.

        Logic:
        1. At each candle, check if price dropped enough to trigger next step
        2. When all filled or price hits TP/SL, close and record trade
        3. Move to next entry point
        """
        candles = self.fetch_candles(symbol, timeframe, days)
        if len(candles) < 10:
            return BacktestResult(
                strategy='martingale', symbol=symbol,
                start_date=candles[0]['timestamp'].isoformat() if candles else '',
                end_date=candles[-1]['timestamp'].isoformat() if candles else '',
                initial_budget=budget, total_trades=0, winning_trades=0,
                losing_trades=0, total_pnl=0, total_pnl_pct=0,
                max_drawdown_pct=0, win_rate=0, avg_win_pct=0,
                avg_loss_pct=0, profit_factor=0
            )

        splits = [0.22, 0.33, 0.45]
        trades: List[BacktestTrade] = []
        equity_curve = [budget]
        peak_equity = budget

        i = 0
        while i < len(candles) - 10:
            entry_price = candles[i]['close']

            # Calculate step prices
            steps = []
            for s in range(3):
                if direction == 'long':
                    price = entry_price * (1 - step_pct / 100 * s)
                    side = 'Buy'
                else:
                    price = entry_price * (1 + step_pct / 100 * s)
                    side = 'Sell'

                amount_usdt = budget * splits[s]
                amount = amount_usdt / price
                steps.append({
                    'price': price, 'side': side,
                    'amount': amount, 'amount_usdt': amount_usdt,
                    'filled': False, 'fill_idx': None
                })

            # TP/SL
            if direction == 'long':
                tp_price = entry_price * (1 + tp_pct / 100)
                sl_price = entry_price * (1 - sl_pct / 100)
            else:
                tp_price = entry_price * (1 - tp_pct / 100)
                sl_price = entry_price * (1 + sl_pct / 100)

            # Simulate fills
            filled_steps = [False, False, False]
            total_cost = 0
            total_qty = 0
            exit_idx = None
            exit_reason = 'timeout'
            max_bars = min(len(candles) - i - 1, 120)  # Max 120 bars hold

            for j in range(i + 1, min(i + 1 + max_bars, len(candles))):
                c = candles[j]
                # Check fills
                for s in range(3):
                    if not filled_steps[s]:
                        if direction == 'long' and c['low'] <= steps[s]['price']:
                            filled_steps[s] = True
                            steps[s]['filled'] = True
                            steps[s]['fill_idx'] = j
                            total_cost += steps[s]['amount_usdt']
                            total_qty += steps[s]['amount']
                        elif direction == 'short' and c['high'] >= steps[s]['price']:
                            filled_steps[s] = True
                            steps[s]['filled'] = True
                            steps[s]['fill_idx'] = j
                            total_cost += steps[s]['amount_usdt']
                            total_qty += steps[s]['amount']

                # Check TP/SL
                if total_qty > 0:
                    avg_entry = total_cost / total_qty
                    if direction == 'long':
                        if c['high'] >= tp_price:
                            exit_price = tp_price
                            pnl = (exit_price - avg_entry) * total_qty
                            exit_reason = 'tp'
                            exit_idx = j
                            break
                        if c['low'] <= sl_price:
                            exit_price = sl_price
                            pnl = (exit_price - avg_entry) * total_qty
                            exit_reason = 'sl'
                            exit_idx = j
                            break
                    else:
                        if c['low'] <= tp_price:
                            exit_price = tp_price
                            pnl = (avg_entry - exit_price) * total_qty
                            exit_reason = 'tp'
                            exit_idx = j
                            break
                        if c['high'] >= sl_price:
                            exit_price = sl_price
                            pnl = (avg_entry - exit_price) * total_qty
                            exit_reason = 'sl'
                            exit_idx = j
                            break

            if exit_idx is None:
                # Timeout - close at last price
                exit_idx = min(i + max_bars, len(candles) - 1)
                exit_price = candles[exit_idx]['close']
                if total_qty > 0:
                    avg_entry = total_cost / total_qty
                    if direction == 'long':
                        pnl = (exit_price - avg_entry) * total_qty
                    else:
                        pnl = (avg_entry - exit_price) * total_qty
                else:
                    pnl = 0
                exit_reason = 'timeout'

            # Record trade
            filled_count = sum(filled_steps)
            if filled_count > 0 and total_qty > 0:
                avg_entry = total_cost / total_qty
                pnl_pct = (pnl / total_cost * 100) if total_cost > 0 else 0

                trades.append(BacktestTrade(
                    step=filled_count, side=direction,
                    entry_price=round(avg_entry, 6),
                    exit_price=round(exit_price, 6),
                    amount=round(total_qty, 8),
                    pnl_usdt=round(pnl, 4),
                    pnl_pct=round(pnl_pct, 2),
                    hold_bars=exit_idx - i,
                    exit_reason=exit_reason,
                ))

                # Update equity
                new_equity = equity_curve[-1] + pnl
                equity_curve.append(new_equity)
                peak_equity = max(peak_equity, new_equity)
            else:
                # No fills, skip
                i += 1
                continue

            # Move to after exit
            i = exit_idx + 1

        # Calculate stats
        if not trades:
            return BacktestResult(
                strategy='martingale', symbol=symbol,
                start_date=candles[0]['timestamp'].isoformat(),
                end_date=candles[-1]['timestamp'].isoformat(),
                initial_budget=budget, total_trades=0, winning_trades=0,
                losing_trades=0, total_pnl=0, total_pnl_pct=0,
                max_drawdown_pct=0, win_rate=0, avg_win_pct=0,
                avg_loss_pct=0, profit_factor=0
            )

        wins = [t for t in trades if t.pnl_usdt > 0]
        losses = [t for t in trades if t.pnl_usdt <= 0]

        total_pnl = sum(t.pnl_usdt for t in trades)
        total_win = sum(t.pnl_usdt for t in wins)
        total_loss = abs(sum(t.pnl_usdt for t in losses))

        # Max drawdown
        max_dd = 0
        peak = equity_curve[0]
        for eq in equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)

        return BacktestResult(
            strategy='martingale', symbol=symbol,
            start_date=candles[0]['timestamp'].isoformat(),
            end_date=candles[-1]['timestamp'].isoformat(),
            initial_budget=budget,
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            total_pnl=round(total_pnl, 4),
            total_pnl_pct=round(total_pnl / budget * 100, 2),
            max_drawdown_pct=round(max_dd, 2),
            win_rate=round(len(wins) / len(trades) * 100, 1) if trades else 0,
            avg_win_pct=round(sum(t.pnl_pct for t in wins) / len(wins), 2) if wins else 0,
            avg_loss_pct=round(sum(t.pnl_pct for t in losses) / len(losses), 2) if losses else 0,
            profit_factor=round(total_win / total_loss, 2) if total_loss > 0 else float('inf'),
            trades=trades,
        )

    def backtest_grid(self, symbol: str, budget: float = 5.0,
                      step_pct: float = 2.5, levels: int = 3,
                      direction: str = 'long',
                      tp_pct: float = 3.0, sl_pct: float = 8.0,
                      timeframe: str = '60', days: int = 30) -> BacktestResult:
        """Backtest grid strategy on historical data"""
        candles = self.fetch_candles(symbol, timeframe, days)
        if len(candles) < 10:
            return BacktestResult(
                strategy='grid', symbol=symbol,
                start_date='', end_date='',
                initial_budget=budget, total_trades=0, winning_trades=0,
                losing_trades=0, total_pnl=0, total_pnl_pct=0,
                max_drawdown_pct=0, win_rate=0, avg_win_pct=0,
                avg_loss_pct=0, profit_factor=0
            )

        per_level = budget / levels
        trades: List[BacktestTrade] = []
        equity_curve = [budget]

        i = 0
        while i < len(candles) - 10:
            entry_price = candles[i]['close']

            # Grid levels
            grid_levels = []
            for lv in range(levels):
                if direction == 'long':
                    price = entry_price * (1 - step_pct / 100 * (lv + 1))
                    side = 'Buy'
                else:
                    price = entry_price * (1 + step_pct / 100 * (lv + 1))
                    side = 'Sell'

                amount = per_level / price
                grid_levels.append({
                    'price': price, 'side': side, 'amount': amount,
                    'cost': per_level, 'filled': False
                })

            # TP/SL
            if direction == 'long':
                tp_price = entry_price * (1 + tp_pct / 100)
                sl_price = entry_price * (1 - sl_pct / 100)
            else:
                tp_price = entry_price * (1 - tp_pct / 100)
                sl_price = entry_price * (1 + sl_pct / 100)

            # Simulate
            total_qty = 0
            total_cost = 0
            exit_idx = None
            exit_price = 0.0
            pnl = 0.0
            exit_reason = 'timeout'
            max_bars = min(len(candles) - i - 1, 120)

            for j in range(i + 1, min(i + 1 + max_bars, len(candles))):
                c = candles[j]

                for lv in grid_levels:
                    if not lv['filled']:
                        if direction == 'long' and c['low'] <= lv['price']:
                            lv['filled'] = True
                            total_qty += lv['amount']
                            total_cost += lv['cost']
                        elif direction == 'short' and c['high'] >= lv['price']:
                            lv['filled'] = True
                            total_qty += lv['amount']
                            total_cost += lv['cost']

                if total_qty > 0:
                    avg = total_cost / total_qty
                    if direction == 'long':
                        if c['high'] >= tp_price:
                            pnl = (tp_price - avg) * total_qty
                            exit_reason = 'tp'; exit_idx = j; break
                        if c['low'] <= sl_price:
                            pnl = (sl_price - avg) * total_qty
                            exit_reason = 'sl'; exit_idx = j; break
                    else:
                        if c['low'] <= tp_price:
                            pnl = (avg - tp_price) * total_qty
                            exit_reason = 'tp'; exit_idx = j; break
                        if c['high'] >= sl_price:
                            pnl = (avg - sl_price) * total_qty
                            exit_reason = 'sl'; exit_idx = j; break

            if exit_idx is None:
                exit_idx = min(i + max_bars, len(candles) - 1)
                exit_price = candles[exit_idx]['close']
                if total_qty > 0:
                    avg = total_cost / total_qty
                    pnl = (exit_price - avg) * total_qty if direction == 'long' else (avg - exit_price) * total_qty
                else:
                    pnl = 0
                exit_reason = 'timeout'

            filled_count = sum(1 for lv in grid_levels if lv['filled'])
            if filled_count > 0 and total_qty > 0:
                avg = total_cost / total_qty
                pnl_pct = (pnl / total_cost * 100) if total_cost > 0 else 0
                # Get actual exit price
                actual_exit = exit_price if exit_price else candles[exit_idx]['close']
                trades.append(BacktestTrade(
                    step=filled_count, side=direction,
                    entry_price=round(avg, 6), exit_price=round(actual_exit, 6),
                    amount=round(total_qty, 8), pnl_usdt=round(pnl, 4),
                    pnl_pct=round(pnl_pct, 2), hold_bars=exit_idx - i,
                    exit_reason=exit_reason,
                ))
                equity_curve.append(equity_curve[-1] + pnl)

            i = (exit_idx or i) + 1

        # Stats
        if not trades:
            return BacktestResult(strategy='grid', symbol=symbol,
                start_date=candles[0]['timestamp'].isoformat(),
                end_date=candles[-1]['timestamp'].isoformat(),
                initial_budget=budget, total_trades=0, winning_trades=0,
                losing_trades=0, total_pnl=0, total_pnl_pct=0,
                max_drawdown_pct=0, win_rate=0, avg_win_pct=0,
                avg_loss_pct=0, profit_factor=0)

        wins = [t for t in trades if t.pnl_usdt > 0]
        losses = [t for t in trades if t.pnl_usdt <= 0]
        total_pnl = sum(t.pnl_usdt for t in trades)
        total_win = sum(t.pnl_usdt for t in wins)
        total_loss = abs(sum(t.pnl_usdt for t in losses))
        max_dd = 0
        peak = equity_curve[0]
        for eq in equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)

        return BacktestResult(
            strategy='grid', symbol=symbol,
            start_date=candles[0]['timestamp'].isoformat(),
            end_date=candles[-1]['timestamp'].isoformat(),
            initial_budget=budget,
            total_trades=len(trades), winning_trades=len(wins),
            losing_trades=len(losses),
            total_pnl=round(total_pnl, 4),
            total_pnl_pct=round(total_pnl / budget * 100, 2),
            max_drawdown_pct=round(max_dd, 2),
            win_rate=round(len(wins) / len(trades) * 100, 1) if trades else 0,
            avg_win_pct=round(sum(t.pnl_pct for t in wins) / len(wins), 2) if wins else 0,
            avg_loss_pct=round(sum(t.pnl_pct for t in losses) / len(losses), 2) if losses else 0,
            profit_factor=round(total_win / total_loss, 2) if total_loss > 0 else float('inf'),
            trades=trades,
        )

    def format_result(self, result: BacktestResult) -> str:
        """Format backtest result as readable report"""
        lines = [
            f"📊 Backtest: {result.strategy.upper()} on {result.symbol}",
            f"Period: {result.start_date[:10]} → {result.end_date[:10]}",
            f"Budget: ${result.initial_budget:.2f}",
            f"─────────────────────",
            f"Trades: {result.total_trades} (W:{result.winning_trades} L:{result.losing_trades})",
            f"Win Rate: {result.win_rate:.1f}%",
            f"Total PnL: ${result.total_pnl:.4f} ({result.total_pnl_pct:+.2f}%)",
            f"Avg Win: +{result.avg_win_pct:.2f}% | Avg Loss: {result.avg_loss_pct:.2f}%",
            f"Profit Factor: {result.profit_factor:.2f}",
            f"Max Drawdown: {result.max_drawdown_pct:.2f}%",
        ]

        # Exit reason breakdown
        if result.trades:
            tp_count = sum(1 for t in result.trades if t.exit_reason == 'tp')
            sl_count = sum(1 for t in result.trades if t.exit_reason == 'sl')
            timeout_count = sum(1 for t in result.trades if t.exit_reason == 'timeout')
            lines.append(f"Exits: TP={tp_count} SL={sl_count} Timeout={timeout_count}")

        return '\n'.join(lines)
