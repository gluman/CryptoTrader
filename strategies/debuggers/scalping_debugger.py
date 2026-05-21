#!/usr/bin/env python3
"""
ScalpingDebugger — backtest and analyze scalping strategy
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.config import Config
from src.core.database import DatabaseManager
from cryptotrader_strategies import ScalpingStrategy
import pandas as pd


class ScalpingDebugger:
    def __init__(self):
        import logging
        self.logger = logging.getLogger('scalping_debugger')
        self.config = Config.load()
        self.db = DatabaseManager(self.config.postgresql, self.logger)
        self.strategy = ScalpingStrategy()
        self.params = self.strategy.get_parameters()

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        with self.db.get_session() as session:
            from src.core.database import OHLCVRaw
            records = session.query(OHLCVRaw).filter(
                OHLCVRaw.exchange.in_(['bybit', 'binance', 'BINANCE', 'Binance']),
                OHLCVRaw.symbol == symbol,
                OHLCVRaw.timeframe == timeframe
            ).order_by(OHLCVRaw.timestamp.desc()).limit(limit).all()

            if not records:
                return pd.DataFrame()

            data = [{
                'timestamp': r.timestamp,
                'open': float(r.open),
                'high': float(r.high),
                'low': float(r.low),
                'close': float(r.close),
                'volume': float(r.volume),
            } for r in reversed(records)]

            return pd.DataFrame(data)

    def backtest(self, symbol: str = 'BTCUSDT', periods: int = 30):
        """Run backtest on last N periods"""
        print(f"\n=== Scalping Backtest: {symbol} ===")

        df_5m = self.get_ohlcv(symbol, '5', limit=500)
        df_1m = self.get_ohlcv(symbol, '1', limit=200)

        if df_5m.empty or len(df_5m) < 50:
            print(f"Not enough data for {symbol}")
            return

        # Simulate trades
        trades = []
        position = None
        entry_price = 0
        entry_time = None

        for i in range(20, len(df_5m)):
            df_5 = df_5m.iloc[:i+1].copy()
            df_1 = df_1m[df_1m['timestamp'] <= df_5.iloc[-1]['timestamp']].copy() if not df_1m.empty else None

            signal = self.strategy.analyze(df_1, df_5, None, None, None, None, {})

            if signal['action'] == 'BUY' and position is None:
                position = 'long'
                entry_price = signal['entry_price']
                entry_time = df_5.iloc[-1]['timestamp']
                print(f"  BUY at {entry_price:.2f} ({entry_time})")

            elif signal['action'] == 'SELL' and position == 'long':
                exit_price = signal['entry_price']
                pnl_pct = (exit_price - entry_price) / entry_price * 100
                trades.append({
                    'entry': entry_price,
                    'exit': exit_price,
                    'pnl_pct': pnl_pct,
                    'duration': (df_5.iloc[-1]['timestamp'] - entry_time).total_seconds() / 60
                })
                print(f"  SELL at {exit_price:.2f} | PnL: {pnl_pct:+.2f}%")
                position = None

            # Check SL/TP
            if position == 'long' and entry_price > 0:
                sl = signal.get('stop_loss', entry_price * 0.998)
                tp = signal.get('take_profit', entry_price * 1.003)
                current = df_5.iloc[-1]['close']

                if current <= sl:
                    trades.append({
                        'entry': entry_price,
                        'exit': sl,
                        'pnl_pct': (sl - entry_price) / entry_price * 100,
                        'duration': 0,
                        'reason': 'SL'
                    })
                    print(f"  SL hit at {sl:.2f}")
                    position = None

        self._analyze_results(trades)

    def _analyze_results(self, trades: list):
        if not trades:
            print("  No trades generated")
            return

        wins = [t for t in trades if t['pnl_pct'] > 0]
        losses = [t for t in trades if t['pnl_pct'] <= 0]

        total_pnl = sum(t['pnl_pct'] for t in trades)
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_win = sum(t['pnl_pct'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['pnl_pct'] for t in losses) / len(losses) if losses else 0
        profit_factor = abs(avg_win * len(wins) / (avg_loss * len(losses))) if losses and avg_loss != 0 else 0

        print(f"\n  Results:")
        print(f"    Total trades: {len(trades)}")
        print(f"    Win rate: {win_rate:.1f}%")
        print(f"    Avg win: {avg_win:+.3f}%")
        print(f"    Avg loss: {avg_loss:+.3f}%")
        print(f"    Profit factor: {profit_factor:.2f}")
        print(f"    Total PnL: {total_pnl:+.2f}%")

        if win_rate < 45 or profit_factor < 1.0:
            print(f"  ⚠️  Strategy needs tuning")

    def analyze_parameters(self):
        """Analyze current parameters and suggest improvements"""
        print(f"\n=== Scalping Parameter Analysis ===")
        p = self.params
        print(f"  SL: {p['max_loss_per_trade']*100:.1f}% | TP: {p['target_profit']*100:.1f}%")
        print(f"  RSI: {p['rsi_oversold']}/{p['rsi_overbought']} (period {p['rsi_fast']})")
        print(f"  Volume spike threshold: {p['volume_spike']}x")
        print(f"  Min confidence: {p['min_confidence']}")

        # Check if params are reasonable for scalping
        if p['max_loss_per_trade'] > 0.005:
            print(f"  ⚠️  SL might be too wide for scalping (consider ≤0.5%)")
        if p['target_profit'] < p['max_loss_per_trade']:
            print(f"  ⚠️  TP should be > SL for positive expectancy")


if __name__ == '__main__':
    import logging
    logger = logging.getLogger('test')
    db = Config.load()

    debugger = ScalpingDebugger()
    debugger.analyze_parameters()
    debugger.backtest('BTCUSDT')
    debugger.backtest('ETHUSDT')
