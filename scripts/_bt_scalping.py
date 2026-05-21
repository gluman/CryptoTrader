#!/usr/bin/env python3
import sys
sys.path.insert(0, '/home/andy')
import psycopg2
import pandas as pd
from cryptotrader_strategies import ScalpingStrategy

conn = psycopg2.connect(host='192.168.0.149', port=5432, dbname='cryptotrader', user='cryptotrader', password='cryptotrader123')

def get_ohlcv(symbol, timeframe, limit=500, exchange='bybit'):
    cur = conn.cursor()
    cur.execute("""
        SELECT timestamp, open, high, low, close, volume
        FROM ohlcv_raw
        WHERE symbol=%s AND timeframe=%s AND exchange=%s
        ORDER BY timestamp DESC
        LIMIT %s
    """, (symbol, timeframe, exchange, limit))
    rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    data = [{'timestamp': r[0], 'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]), 'close': float(r[4]), 'volume': float(r[5])} for r in reversed(rows)]
    return pd.DataFrame(data)

def backtest(symbol):
    print(f"\n=== Scalping Backtest: {symbol} ===")

    df_5m = get_ohlcv(symbol, '5m', limit=500)
    df_1m = get_ohlcv(symbol, '1m', limit=200)

    print(f"  5m candles: {len(df_5m)}, 1m candles: {len(df_1m)}")

    if df_5m.empty or len(df_5m) < 50:
        print(f"  Not enough data for {symbol}")
        return

    strategy = ScalpingStrategy()
    params = strategy.get_parameters()

    print(f"\n=== Scalping Parameter Analysis ===")
    print(f"  SL: {params['max_loss_per_trade']*100:.1f}% | TP: {params['target_profit']*100:.1f}%")
    print(f"  RSI: {params['rsi_oversold']}/{params['rsi_overbought']} (period {params['rsi_fast']})")
    print(f"  Volume spike threshold: {params['volume_spike']}x")
    print(f"  Min confidence: {params['min_confidence']}")

    if params['max_loss_per_trade'] > 0.005:
        print(f"  WARNING: SL might be too wide for scalping (consider <=0.5%)")
    if params['target_profit'] < params['max_loss_per_trade']:
        print(f"  WARNING: TP should be > SL for positive expectancy")

    sl_pct = params['max_loss_per_trade']
    tp_pct = params['target_profit']

    trades = []
    position = None
    entry_price = 0
    entry_time = None

    for i in range(20, len(df_5m)):
        df_5 = df_5m.iloc[:i+1].copy()
        df_1 = df_1m[df_1m['timestamp'] <= df_5.iloc[-1]['timestamp']].copy() if not df_1m.empty else None

        signal = strategy.analyze(df_1, df_5, None, None, None, None, {})

        if signal['action'] == 'BUY' and position is None:
            position = 'long'
            entry_price = signal.get('entry_price', df_5.iloc[-1]['close'])
            entry_time = df_5.iloc[-1]['timestamp']
            print(f"  BUY at {entry_price:.2f} ({entry_time})")

        elif signal['action'] == 'SELL' and position == 'long':
            exit_price = signal.get('entry_price', df_5.iloc[-1]['close'])
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            duration = (df_5.iloc[-1]['timestamp'] - entry_time).total_seconds() / 60 if entry_time else 0
            trades.append({'entry': entry_price, 'exit': exit_price, 'pnl_pct': pnl_pct, 'duration': duration, 'reason': 'SIGNAL'})
            print(f"  SELL at {exit_price:.2f} | PnL: {pnl_pct:+.3f}% | {duration:.0f}min")
            position = None

        if position == 'long' and entry_price > 0:
            high = df_5.iloc[-1]['high']
            low = df_5.iloc[-1]['low']
            sl = entry_price * (1 - sl_pct)
            tp = entry_price * (1 + tp_pct)

            if low <= sl:
                trades.append({'entry': entry_price, 'exit': sl, 'pnl_pct': -sl_pct * 100, 'duration': 0, 'reason': 'SL'})
                print(f"  SL hit at {sl:.2f}")
                position = None
            elif high >= tp:
                trades.append({'entry': entry_price, 'exit': tp, 'pnl_pct': tp_pct * 100, 'duration': 0, 'reason': 'TP'})
                print(f"  TP hit at {tp:.2f}")
                position = None

    if not trades:
        print("\n  No trades generated")
        return

    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] <= 0]

    total_pnl = sum(t['pnl_pct'] for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t['pnl_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl_pct'] for t in losses) / len(losses) if losses else 0
    sum_wins = sum(t['pnl_pct'] for t in wins)
    sum_losses = abs(sum(t['pnl_pct'] for t in losses))
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else 0

    print(f"\n  Results:")
    print(f"    Total trades: {len(trades)}")
    print(f"    Win rate: {win_rate:.1f}%")
    print(f"    Avg win: {avg_win:+.3f}%")
    print(f"    Avg loss: {avg_loss:+.3f}%")
    print(f"    Profit factor: {profit_factor:.2f}")
    print(f"    Total PnL: {total_pnl:+.2f}%")

    sl_hits = [t for t in trades if t.get('reason') == 'SL']
    tp_hits = [t for t in trades if t.get('reason') == 'TP']
    sig_exits = [t for t in trades if t.get('reason') == 'SIGNAL']
    print(f"    SL hits: {len(sl_hits)}, TP hits: {len(tp_hits)}, Signal exits: {len(sig_exits)}")

    if win_rate < 45 or profit_factor < 1.0:
        print(f"  STATUS: Needs tuning")
    else:
        print(f"  STATUS: Viable")

backtest('BTCUSDT')
backtest('ETHUSDT')
conn.close()
