#!/usr/bin/env python3
import sys
sys.path.insert(0, '/home/andy')

from src.core.config import Config
from src.core.database import DatabaseManager
from cryptotrader_strategies import PositionStrategy
import pandas as pd
import logging

logging.basicConfig(level=logging.WARNING)

config = Config.load()
db = DatabaseManager(config.postgresql, logging.getLogger('test'))
strategy = PositionStrategy()
params = strategy.get_parameters()

print('=== Position Parameter Analysis ===')
print(f"  SL: {params['max_loss_per_trade']*100:.1f}% | TP: {params['target_profit']*100:.1f}%")
print(f"  RSI: {params['rsi_oversold']}/{params['rsi_overbought']} (strong: {params['rsi_strong_oversold']}/{params['rsi_strong_overbought']})")
print(f"  SMA: {params['sma_20']}/{params['sma_50']}/{params['sma_200']}")
print(f"  Min confidence: {params['min_confidence']}")

def get_ohlcv(symbol, timeframe, limit=500):
    with db.get_session() as session:
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

def backtest(symbol):
    print(f'\n=== Position Backtest: {symbol} ===')
    df_4h = get_ohlcv(symbol, '4h', limit=600)
    if df_4h.empty or len(df_4h) < 50:
        print(f'Not enough 4h data for {symbol}')
        return
    
    df_1h = get_ohlcv(symbol, '1h', limit=600)
    
    trades = []
    position = None
    entry_price = 0
    entry_time = None
    
    for i in range(50, len(df_4h)):
        df_d = df_4h.iloc[:i+1].copy()
        df_4 = df_1h[df_1h['timestamp'] <= df_d.iloc[-1]['timestamp']].copy() if not df_1h.empty else None
        
        signal = strategy.analyze(None, None, None, None, df_4, df_d, {})
        
        if signal['action'] == 'BUY' and position is None:
            position = 'long'
            entry_price = signal['entry_price']
            entry_time = df_d.iloc[-1]['timestamp']
            print(f"  BUY at {entry_price:.2f}")
        elif signal['action'] == 'SELL' and position == 'long':
            exit_price = signal['entry_price']
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            duration_days = (df_d.iloc[-1]['timestamp'] - entry_time).days
            trades.append({
                'entry': entry_price, 'exit': exit_price, 'pnl_pct': pnl_pct,
                'duration_days': duration_days
            })
            print(f"  SELL at {exit_price:.2f} | PnL: {pnl_pct:+.2f}% | {duration_days}d")
            position = None
        elif position == 'long' and entry_price > 0:
            current = df_d.iloc[-1]['close']
            sl = signal.get('stop_loss', entry_price * (1 - params['max_loss_per_trade']))
            tp = signal.get('take_profit', entry_price * (1 + params['target_profit']))
            if current <= sl:
                pnl_pct = (sl - entry_price) / entry_price * 100
                trades.append({'entry': entry_price, 'exit': sl, 'pnl_pct': pnl_pct, 'reason': 'SL'})
                print(f"  SL hit at {sl:.2f} ({pnl_pct:+.2f}%)")
                position = None
            elif current >= tp:
                pnl_pct = (tp - entry_price) / entry_price * 100
                trades.append({'entry': entry_price, 'exit': tp, 'pnl_pct': pnl_pct, 'reason': 'TP'})
                print(f"  TP hit at {tp:.2f} ({pnl_pct:+.2f}%)")
                position = None
    
    if not trades:
        print('  No trades generated')
        return
    
    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] <= 0]
    total_pnl = sum(t['pnl_pct'] for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t['pnl_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl_pct'] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(avg_win * len(wins) / (avg_loss * len(losses))) if losses and avg_loss != 0 else float('inf') if wins and not losses else 0
    avg_duration = sum(t.get('duration_days', 0) for t in trades) / len(trades)
    
    print(f'\n  Results:')
    print(f"    Total trades: {len(trades)}")
    print(f"    Win rate: {win_rate:.1f}%")
    print(f"    Avg win: {avg_win:+.2f}%")
    print(f"    Avg loss: {avg_loss:+.2f}%")
    print(f"    Profit factor: {profit_factor:.2f}")
    print(f"    Total PnL: {total_pnl:+.2f}%")
    print(f"    Avg duration: {avg_duration:.1f} days")
    if win_rate < 35 or profit_factor < 1.2:
        print(f'  Strategy needs tuning')

backtest('BTCUSDT')
backtest('ETHUSDT')
