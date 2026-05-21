#!/usr/bin/env python3
import sys, os
sys.path.insert(0, '/home/andy')

from src.core.config import Config
from src.core.database import DatabaseManager, OHLCVRaw
import logging
import pandas as pd

logger = logging.getLogger('test')
config = Config.load()
db = DatabaseManager(config.postgresql, logger)

def get_ohlcv(symbol, timeframe):
    tf_map = {'1d': '1', '4h': '240'}
    tf = tf_map.get(timeframe, timeframe)
    with db.get_session() as session:
        records = session.query(OHLCVRaw).filter(
            OHLCVRaw.exchange == 'bybit',
            OHLCVRaw.symbol == symbol,
            OHLCVRaw.timeframe == tf
        ).order_by(OHLCVRaw.timestamp.asc()).all()
        if not records:
            return pd.DataFrame()
        data = [{
            'timestamp': r.timestamp,
            'open': float(r.open),
            'high': float(r.high),
            'low': float(r.low),
            'close': float(r.close),
            'volume': float(r.volume),
        } for r in records]
        return pd.DataFrame(data)

from cryptotrader_strategies import PositionStrategy
strategy = PositionStrategy()
params = strategy.get_parameters()

print('=== Position Parameter Analysis ===')
print(f"  SL: {params['max_loss_per_trade']*100:.1f}% | TP: {params['target_profit']*100:.1f}%")
print(f"  RSI: {params['rsi_oversold']}/{params['rsi_overbought']} (strong: {params['rsi_strong_oversold']}/{params['rsi_strong_overbought']})")
print(f"  SMA: {params['sma_20']}/{params['sma_50']}/{params['sma_200']}")
print(f"  Min confidence: {params['min_confidence']}")

for symbol in ['BTCUSDT', 'ETHUSDT']:
    print(f'\n=== Position Backtest: {symbol} ===')
    df_1 = get_ohlcv(symbol, '1d')
    df_240 = get_ohlcv(symbol, '4h')
    print(f"  Data: 1d={len(df_1)} rows, 4h={len(df_240)} rows")
    
    if df_1.empty or len(df_1) < 50:
        print(f"  Not enough data for {symbol}")
        continue
    
    trades = []
    position = None
    entry_price = 0
    entry_time = None
    
    for i in range(50, len(df_1)):
        df_d = df_1.iloc[:i+1].copy()
        ts = df_d.iloc[-1]['timestamp']
        df_4 = df_240[df_240['timestamp'] <= ts].copy() if not df_240.empty else None
        
        signal = strategy.analyze(None, None, None, None, df_4, df_d, {})
        
        # Only open position on BUY when flat
        if signal['action'] == 'BUY' and position is None:
            position = 'long'
            entry_price = signal['entry_price']
            entry_time = ts
            print(f"  BUY at {entry_price:.2f} on {ts}")
        
        # Only close on SELL when in position
        elif signal['action'] == 'SELL' and position == 'long':
            exit_price = signal['entry_price']
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            dur = (ts - entry_time).days if hasattr((ts - entry_time), 'days') else 0
            trades.append({'entry': entry_price, 'exit': exit_price, 'pnl_pct': pnl_pct, 'duration_days': dur})
            print(f"  SELL at {exit_price:.2f} | PnL: {pnl_pct:+.2f}% | {dur}d")
            position = None
        
        # Check SL/TP for open position - use the candle's high/low for intrabar
        if position == 'long' and entry_price > 0:
            high = df_d.iloc[-1]['high']
            low = df_d.iloc[-1]['low']
            sl = entry_price * (1 - params['max_loss_per_trade'])
            tp = entry_price * (1 + params['target_profit'])
            # Check if SL or TP was hit during this candle
            if low <= sl:
                trades.append({'entry': entry_price, 'exit': sl, 'pnl_pct': (sl - entry_price) / entry_price * 100, 'reason': 'SL'})
                print(f"  SL hit at {sl:.2f} (low={low:.2f})")
                position = None
            elif high >= tp:
                trades.append({'entry': entry_price, 'exit': tp, 'pnl_pct': (tp - entry_price) / entry_price * 100, 'reason': 'TP'})
                print(f"  TP hit at {tp:.2f} (high={high:.2f})")
                position = None
    
    # Close any remaining position at last close
    if position == 'long':
        last_close = df_1.iloc[-1]['close']
        last_ts = df_1.iloc[-1]['timestamp']
        pnl_pct = (last_close - entry_price) / entry_price * 100
        dur = (last_ts - entry_time).days
        trades.append({'entry': entry_price, 'exit': last_close, 'pnl_pct': pnl_pct, 'duration_days': dur, 'reason': 'END'})
        print(f"  Closed at end: {last_close:.2f} | PnL: {pnl_pct:+.2f}% | {dur}d")
    
    if not trades:
        print("  No trades generated")
    else:
        wins = [t for t in trades if t['pnl_pct'] > 0]
        losses = [t for t in trades if t['pnl_pct'] <= 0]
        total_pnl = sum(t['pnl_pct'] for t in trades)
        win_rate = len(wins) / len(trades) * 100
        avg_win = sum(t['pnl_pct'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['pnl_pct'] for t in losses) / len(losses) if losses else 0
        pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if losses and avg_loss != 0 else float('inf') if wins and not losses else 0
        avg_dur = sum(t.get('duration_days', 0) for t in trades) / len(trades)
        
        print(f"\n  Results:")
        print(f"    Total trades: {len(trades)}")
        print(f"    Win rate: {win_rate:.1f}%")
        print(f"    Avg win: {avg_win:+.2f}%")
        print(f"    Avg loss: {avg_loss:+.2f}%")
        print(f"    Profit factor: {pf:.2f}")
        print(f"    Total PnL: {total_pnl:+.2f}%")
        print(f"    Avg duration: {avg_dur:.1f} days")
        if win_rate < 35 or pf < 1.2:
            print(f"  WARNING: Strategy needs tuning")
        else:
            print(f"  Strategy looks viable")
