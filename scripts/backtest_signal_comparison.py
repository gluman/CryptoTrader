#!/usr/bin/env python3
"""
Backtest: Old signal logic (RSI<30, min_conf=0.35) vs New logic (RSI<35, min_conf=0.25)
Compares signal counts and profitability on last 30 days 5m OHLCV for BTCUSDT, ETHUSDT, SOLUSDT
"""
import sys
sys.path.insert(0, '/home/andy')

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger('backtest')

# PostgreSQL connection params
DB_PARAMS = {
    'host': '192.168.0.149',
    'port': 5432,
    'database': 'cryptotrader',
    'user': 'cryptotrader',
    'password': 'cryptotrader123'
}

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
TIMEFRAME = '5m'
DAYS_BACK = 30


def get_ohlcv_from_db(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """Fetch OHLCV data from PostgreSQL"""
    import psycopg2
    conn = psycopg2.connect(**DB_PARAMS)
    
    cutoff = datetime.now() - timedelta(days=days)
    
    query = """
        SELECT timestamp, open, high, low, close, volume
        FROM ohlcv_raw
        WHERE symbol = %s AND timeframe = %s AND timestamp >= %s
        ORDER BY timestamp ASC
    """
    
    df = pd.read_sql_query(query, conn, params=(symbol, timeframe, cutoff))
    conn.close()
    
    if df.empty:
        return pd.DataFrame()
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute RSI, CSS, BB position, SMA20/50, MACD, FDI regime"""
    df = df.copy()
    close = df['close']
    
    # RSI(14)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI_14'] = 100 - (100 / (1 + rs))
    
    # CSS (Cointegration Signal Strength proxy - use EMA cross angle)
    ema_fast = close.ewm(span=5, adjust=False).mean()
    ema_slow = close.ewm(span=20, adjust=False).mean()
    df['ema_fast'] = ema_fast
    df['ema_slow'] = ema_slow
    css_raw = (ema_fast - ema_slow) / ema_slow * 100
    df['CSS'] = css_raw.rolling(5).mean()  # smoothed CSS
    
    # BB position
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    df['BB_position'] = (close - bb_lower) / (bb_upper - bb_lower)
    df['SMA20'] = sma20
    df['SMA50'] = close.rolling(50).mean()
    
    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    df['MACD'] = macd_line
    df['MACD_signal'] = signal_line
    df['MACD_hist'] = macd_line - signal_line
    
    # FDI regime (Fractal Dimension Index proxy using BB width)
    bb_width = (bb_upper - bb_lower) / sma20
    df['FDI'] = bb_width.rolling(20).mean() / bb_width
    
    return df


def simulate_signals_old(df: pd.DataFrame, rsi_thresh: float = 30, min_conf: float = 0.35) -> List[Dict]:
    """
    OLD logic: RSI < 30 → BUY, RSI > 70 → SELL
    Only count signals when confidence >= 0.35 (min_conf threshold)
    """
    signals = []
    position = None
    
    for i in range(50, len(df)):
        row = df.iloc[i]
        rsi = row['RSI_14']
        css = row['CSS']
        css_prior = df.iloc[i-1]['CSS'] if i > 0 else css
        price = row['close']
        
        if pd.isna(rsi) or pd.isna(css):
            continue
        
        buy_signal = False
        sell_signal = False
        confidence = 0.0
        
        # RSI-based buy
        if rsi < rsi_thresh and position is None:
            buy_signal = True
            confidence = 0.50  # base confidence for RSI signal
            
        # RSI-based sell
        elif rsi > (100 - rsi_thresh) and position is None:
            sell_signal = True
            confidence = 0.50
            
        # CSS cross up + RSI < 60
        elif css > css_prior and css < 0 and rsi < 60 and position is None:
            buy_signal = True
            confidence = 0.40
            
        # CSS cross down + RSI > 40
        elif css < css_prior and css > 0 and rsi > 40 and position is None:
            sell_signal = True
            confidence = 0.40
        
        if confidence < min_conf:
            continue
            
        if buy_signal:
            signals.append({
                'bar': i,
                'timestamp': row['timestamp'],
                'action': 'BUY',
                'price': price,
                'rsi': rsi,
                'confidence': confidence,
                'logic': 'old'
            })
            position = 'long'
        elif sell_signal:
            signals.append({
                'bar': i,
                'timestamp': row['timestamp'],
                'action': 'SELL',
                'price': price,
                'rsi': rsi,
                'confidence': confidence,
                'logic': 'old'
            })
            position = None
    
    return signals


def simulate_signals_new(df: pd.DataFrame, rsi_thresh_low: float = 35, rsi_thresh_high: float = 65, min_conf: float = 0.25) -> List[Dict]:
    """
    NEW logic: RSI < 35 → BUY (boosted conf=0.50), RSI > 65 → SELL (boosted conf=0.50)
    CSS cross rules + min_conf=0.25
    """
    signals = []
    position = None
    
    for i in range(50, len(df)):
        row = df.iloc[i]
        rsi = row['RSI_14']
        css = row['CSS']
        css_prior = df.iloc[i-1]['CSS'] if i > 0 else css
        price = row['close']
        
        if pd.isna(rsi) or pd.isna(css):
            continue
        
        buy_signal = False
        sell_signal = False
        confidence = 0.0
        
        # RSI-based buy with BOOST
        if rsi < rsi_thresh_low and position is None:
            buy_signal = True
            confidence = 0.50  # BOOSTED confidence
            
        # RSI-based sell with BOOST
        elif rsi > rsi_thresh_high and position is None:
            sell_signal = True
            confidence = 0.50  # BOOSTED confidence
            
        # CSS cross up + RSI < 60
        elif css > css_prior and css < 0 and rsi < 60 and position is None:
            buy_signal = True
            confidence = 0.40
            
        # CSS cross down + RSI > 40
        elif css < css_prior and css > 0 and rsi > 40 and position is None:
            sell_signal = True
            confidence = 0.40
        
        if confidence < min_conf:
            continue
            
        if buy_signal:
            signals.append({
                'bar': i,
                'timestamp': row['timestamp'],
                'action': 'BUY',
                'price': price,
                'rsi': rsi,
                'confidence': confidence,
                'logic': 'new'
            })
            position = 'long'
        elif sell_signal:
            signals.append({
                'bar': i,
                'timestamp': row['timestamp'],
                'action': 'SELL',
                'price': price,
                'rsi': rsi,
                'confidence': confidence,
                'logic': 'new'
            })
            position = None
    
    return signals


def evaluate_profitability(df: pd.DataFrame, signals: List[Dict], lookback: int = 5) -> Tuple[List[Dict], Dict]:
    """
    Check if signal was profitable: price goes up in next 'lookback' bars for BUY
    Returns enriched signals with profitability info
    """
    enriched = []
    stats = {'total': 0, 'profitable': 0, 'loss': 0, 'neutral': 0}
    
    for sig in signals:
        bar = sig['bar']
        action = sig['action']
        entry_price = sig['price']
        
        if bar + lookback >= len(df):
            continue
        
        # Next 5 bars
        future_prices = df.iloc[bar+1:bar+1+lookback]['close']
        if future_prices.empty:
            continue
        
        exit_price = future_prices.iloc[-1]
        price_change = (exit_price - entry_price) / entry_price * 100
        
        sig['exit_price'] = exit_price
        sig['price_change_pct'] = price_change
        sig['profitable'] = price_change > 0 if action == 'BUY' else price_change < 0
        
        stats['total'] += 1
        if sig['profitable']:
            stats['profitable'] += 1
        elif price_change < 0:
            stats['loss'] += 1
        else:
            stats['neutral'] += 1
            
        enriched.append(sig)
    
    stats['win_rate'] = stats['profitable'] / stats['total'] * 100 if stats['total'] > 0 else 0
    
    return enriched, stats


def run_backtest():
    results = {}
    
    for symbol in SYMBOLS:
        logger.info(f"\n{'='*60}")
        logger.info(f"  {symbol} - Backtest Comparison")
        logger.info(f"{'='*60}")
        
        df = get_ohlcv_from_db(symbol, TIMEFRAME, DAYS_BACK)
        
        if df.empty:
            logger.info(f"  No data for {symbol}")
            continue
            
        logger.info(f"  Data: {len(df)} bars | {df['timestamp'].min()} → {df['timestamp'].max()}")
        
        df = compute_indicators(df)
        
        # OLD logic signals
        old_signals = simulate_signals_old(df, rsi_thresh=30, min_conf=0.35)
        old_enriched, old_stats = evaluate_profitability(df, old_signals)
        
        # NEW logic signals
        new_signals = simulate_signals_new(df, rsi_thresh_low=35, rsi_thresh_high=65, min_conf=0.25)
        new_enriched, new_stats = evaluate_profitability(df, new_signals)
        
        # Extra signals from new logic
        extra_signals = len(new_signals) - len(old_signals)
        
        logger.info(f"\n  OLD logic (RSI<30, min_conf=0.35):")
        logger.info(f"    BUY signals:  {sum(1 for s in old_signals if s['action']=='BUY')}")
        logger.info(f"    SELL signals: {sum(1 for s in old_signals if s['action']=='SELL')}")
        logger.info(f"    Total signals: {len(old_signals)}")
        logger.info(f"    Win rate: {old_stats['win_rate']:.1f}% ({old_stats['profitable']}/{old_stats['total']})")
        
        logger.info(f"\n  NEW logic (RSI<35, min_conf=0.25):")
        logger.info(f"    BUY signals:  {sum(1 for s in new_signals if s['action']=='BUY')}")
        logger.info(f"    SELL signals: {sum(1 for s in new_signals if s['action']=='SELL')}")
        logger.info(f"    Total signals: {len(new_signals)}")
        logger.info(f"    Win rate: {new_stats['win_rate']:.1f}% ({new_stats['profitable']}/{new_stats['total']})")
        
        logger.info(f"\n  COMPARISON:")
        logger.info(f"    Extra signals: {extra_signals:+d}")
        logger.info(f"    Win rate delta: {new_stats['win_rate'] - old_stats['win_rate']:+.1f}pp")
        
        # RSI distribution analysis
        rsi_vals = df['RSI_14'].dropna()
        in_old_range = (rsi_vals < 30).sum()
        in_new_range = (rsi_vals < 35).sum()
        logger.info(f"\n  RSI Distribution (all bars):")
        logger.info(f"    RSI < 30: {in_old_range} bars ({in_old_range/len(rsi_vals)*100:.1f}%)")
        logger.info(f"    RSI < 35: {in_new_range} bars ({in_new_range/len(rsi_vals)*100:.1f}%)")
        
        # Meaningful improvement check
        days_span = (df['timestamp'].max() - df['timestamp'].min()).days or 1
        signals_per_day = len(new_signals) / days_span
        meaningful = signals_per_day >= 1
        
        logger.info(f"\n  Meaningfulness: {signals_per_day:.1f} signals/day → {'✓ MEANINGFUL (≥1/day)' if meaningful else '✗ Not meaningful'}")
        
        results[symbol] = {
            'old_count': len(old_signals),
            'new_count': len(new_signals),
            'extra_signals': extra_signals,
            'old_win_rate': old_stats['win_rate'],
            'new_win_rate': new_stats['win_rate'],
            'old_profitable': old_stats['profitable'],
            'old_total': old_stats['total'],
            'new_profitable': new_stats['profitable'],
            'new_total': new_stats['total'],
            'signals_per_day': signals_per_day,
            'meaningful': meaningful
        }
    
    # Summary table
    logger.info(f"\n{'='*80}")
    logger.info(f"  SUMMARY: Signals with New vs Old Rules")
    logger.info(f"{'='*80}")
    logger.info(f"  {'Symbol':<12} {'Old Signals':>12} {'New Signals':>12} {'Extra':>8} {'Old Win%':>10} {'New Win%':>10} {'Meaningful':>12}")
    logger.info(f"  {'-'*12} {'-'*12} {'-'*12} {'-'*8} {'-'*10} {'-'*10} {'-'*12}")
    
    for symbol, r in results.items():
        flag = "✓" if r['meaningful'] else "✗"
        logger.info(f"  {symbol:<12} {r['old_count']:>12} {r['new_count']:>12} {r['extra_signals']:>+8} {r['old_win_rate']:>9.1f}% {r['new_win_rate']:>9.1f}% {flag:>12}")
    
    total_old = sum(r['old_count'] for r in results.values())
    total_new = sum(r['new_count'] for r in results.values())
    avg_old_wr = sum(r['old_win_rate']*r['old_total'] for r in results.values()) / max(1, sum(r['old_total'] for r in results.values()))
    avg_new_wr = sum(r['new_win_rate']*r['new_total'] for r in results.values()) / max(1, sum(r['new_total'] for r in results.values()))
    
    logger.info(f"  {'-'*12} {'-'*12} {'-'*12} {'-'*8} {'-'*10} {'-'*10} {'-'*12}")
    logger.info(f"  {'TOTAL':<12} {total_old:>12} {total_new:>12} {total_new-total_old:>+8} {avg_old_wr:>9.1f}% {avg_new_wr:>9.1f}%")
    
    logger.info(f"\n  CONCLUSION:")
    if total_new > total_old:
        logger.info(f"  ✓ New logic generates {total_new - total_old} MORE signals ({total_new}/{total_old} = {total_new/total_old:.1f}x)")
    else:
        logger.info(f"  ✗ New logic generates FEWER signals")
    
    meaningful_count = sum(1 for r in results.values() if r['meaningful'])
    logger.info(f"  ✓ {meaningful_count}/{len(results)} symbols have ≥1 signal/day (meaningful improvement)")
    
    return results


if __name__ == '__main__':
    run_backtest()