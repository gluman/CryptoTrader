#!/usr/bin/env python3
"""
IntradayDebugger — backtest and analyze intraday strategy
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.config import Config
from src.core.database import DatabaseManager
from cryptotrader_strategies import IntradayStrategy
import pandas as pd


class IntradayDebugger:
    def __init__(self):
        import logging
        self.logger = logging.getLogger('intraday_debugger')
        self.config = Config.load()
        self.db = DatabaseManager(self.config.postgresql, self.logger)
        self.strategy = IntradayStrategy()
        self.params = self.strategy.get_parameters()

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        with self.db.get_session() as session:
            from src.core.database import OHLCVRaw
            records = session.query(OHLCVRaw).filter(
                OHLCVRaw.exchange.in_(['binance', 'BINANCE', 'Binance']),
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

    def backtest(self, symbol: str = 'BTCUSDT', periods: int = 20):
        print(f"\n=== Intraday Backtest: {symbol} ===")

        df_1h = self.get_ohlcv(symbol, '1h', limit=500)
        df_4h = self.get_ohlcv(symbol, '4h', limit=200)

        if df_1h.empty or len(df_1h) < 50:
            print(f"Not enough data for {symbol}")
            return

        trades = []
        position = None
        entry_price = 0
        entry_time = None

        for i in range(50, len(df_1h)):
            df_1 = df_1h.iloc[:i+1].copy()
            df_4 = df_4h[df_4h['timestamp'] <= df_1.iloc[-1]['timestamp']].copy() if not df_4h.empty else None

            signal = self.strategy.analyze(None, None, None, df_1, df_4, None, {})

            if signal['action'] == 'BUY' and position is None:
                position = 'long'
                entry_price = signal['entry_price']
                entry_time = df_1.iloc[-1]['timestamp']
                print(f"  BUY at {entry_price:.2f}")

            elif signal['action'] == 'SELL' and position == 'long':
                exit_price = signal['entry_price']
                pnl_pct = (exit_price - entry_price) / entry_price * 100
                trades.append({
                    'entry': entry_price,
                    'exit': exit_price,
                    'pnl_pct': pnl_pct,
                    'duration': (df_1.iloc[-1]['timestamp'] - entry_time).total_seconds() / 3600
                })
                print(f"  SELL at {exit_price:.2f} | PnL: {pnl_pct:+.2f}%")
                position = None

            # Check SL
            if position == 'long' and entry_price > 0:
                current = df_1.iloc[-1]['close']
                sl = signal.get('stop_loss', entry_price * 0.985)
                tp = signal.get('take_profit', entry_price * 1.025)

                if current <= sl:
                    trades.append({
                        'entry': entry_price,
                        'exit': sl,
                        'pnl_pct': (sl - entry_price) / entry_price * 100,
                        'reason': 'SL'
                    })
                    print(f"  SL hit at {sl:.2f}")
                    position = None
                elif current >= tp:
                    trades.append({
                        'entry': entry_price,
                        'exit': tp,
                        'pnl_pct': (tp - entry_price) / entry_price * 100,
                        'reason': 'TP'
                    })
                    print(f"  TP hit at {tp:.2f}")
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
        print(f"    Avg win: {avg_win:+.2f}%")
        print(f"    Avg loss: {avg_loss:+.2f}%")
        print(f"    Profit factor: {profit_factor:.2f}")
        print(f"    Total PnL: {total_pnl:+.2f}%")

        if win_rate < 40 or profit_factor < 1.1:
            print(f"  ⚠️  Strategy needs tuning")

    def analyze_parameters(self):
        print(f"\n=== Intraday Parameter Analysis ===")
        p = self.params
        print(f"  SL: {p['max_loss_per_trade']*100:.1f}% | TP: {p['target_profit']*100:.1f}%")
        print(f"  RSI: {p['rsi_oversold']}/{p['rsi_overbought']}")
        print(f"  MACD: {p['macd_fast']}/{p['macd_slow']}/{p['macd_signal']}")
        print(f"  EMA: {p['ema_20']}/{p['ema_50']}")
        print(f"  Min confidence: {p['min_confidence']}")


if __name__ == '__main__':
    debugger = IntradayDebugger()
    debugger.analyze_parameters()
    debugger.backtest('BTCUSDT')
    debugger.backtest('ETHUSDT')
