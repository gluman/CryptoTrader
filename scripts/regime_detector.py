#!/usr/bin/env python3
"""Market regime detector for CryptoTrader.
Classifies current market state into one of 6 regimes using 1h OHLCV data.
Computes regime-aware indicator weights for LLM decision-making.

Usage:
    python scripts/regime_detector.py [--symbol BTCUSDT] [--json]

DB: PostgreSQL 192.168.0.149:5432/cryptotrader
"""
import json
import sys
import math
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

DB_CONFIG = dict(host='192.168.0.149', port=5432, database='cryptotrader',
                 user='cryptotrader', password='cryptotrader123')

REGIMES_FILE = Path(__file__).parent.parent / 'data' / 'market_regimes.json'


# ─── Indicator computation ──────────────────────────────────────────────

def _ema(series, period):
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(high, low, close, period=14):
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _adx(high, low, close, period=14):
    """Average Directional Index."""
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    atr_vals = _atr(high, low, close, period)
    plus_di = 100 * _ema(plus_dm, period) / atr_vals.replace(0, np.nan)
    minus_di = 100 * _ema(minus_dm, period) / atr_vals.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _ema(dx, period)


def _bollinger(close, period=20, std_mult=2):
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    return upper, sma, lower


def _psar(high, low, af_start=0.02, af_step=0.02, af_max=0.2):
    """Parabolic SAR (Welles Wilder)."""
    n = len(high)
    psar = np.zeros(n)
    psar[0] = low.iloc[0]
    bull = True
    af = af_start
    ep = high.iloc[0]
    for i in range(1, n):
        prev = psar[i-1]
        new = prev + af * (ep - prev)
        if bull:
            new = min(new, low.iloc[i-1], low.iloc[i])
            if low.iloc[i] < new:
                bull = False
                new = ep
                af = af_start
                ep = low.iloc[i]
        else:
            new = max(new, high.iloc[i-1], high.iloc[i])
            if high.iloc[i] > new:
                bull = True
                new = ep
                af = af_start
                ep = high.iloc[i]
        psar[i] = new
        if bull and high.iloc[i] > ep:
            ep = high.iloc[i]
            af = min(af + af_step, af_max)
        elif not bull and low.iloc[i] < ep:
            ep = low.iloc[i]
            af = min(af + af_step, af_max)
    return pd.Series(psar, index=high.index)


# ─── Data fetching ──────────────────────────────────────────────────────

def _fetch_from_db(symbol, timeframe='1h', limit=500):
    """Fetch OHLCV from PostgreSQL (ohlcv_processed first, fallback to ohlcv_raw)."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    
    # Try ohlcv_processed first
    cur.execute("""
        SELECT timestamp, open, high, low, close, volume,
               rsi_14, macd, macd_signal, macd_hist, atr_14,
               sma_20, sma_50, sma_200, ema_12, ema_26,
               bollinger_upper, bollinger_middle, bollinger_lower,
               volume_ratio, css_value
        FROM ohlcv_processed
        WHERE exchange = 'bybit' AND symbol = %s AND timeframe = %s
        ORDER BY timestamp DESC LIMIT %s
    """, (symbol, timeframe, limit))
    rows = cur.fetchall()
    
    if len(rows) < 50:
        # Fallback to ohlcv_raw (no indicators)
        cur.execute("""
            SELECT timestamp, open, high, low, close, volume
            FROM ohlcv_raw
            WHERE exchange = 'bybit' AND symbol = %s AND timeframe = %s
            ORDER BY timestamp DESC LIMIT %s
        """, (symbol, timeframe, limit))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        if len(rows) < 50:
            return None, False
        
        # Build DataFrame from raw data
        rows.reverse()
        df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        for c in ['open', 'high', 'low', 'close', 'volume']:
            df[c] = df[c].astype(float)
        return df, True  # raw=True means indicators need computing
    
    cur.close()
    conn.close()
    
    import pandas as pd
    rows.reverse()
    cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume',
            'rsi_14', 'macd', 'macd_signal', 'macd_hist', 'atr_14',
            'sma_20', 'sma_50', 'sma_200', 'ema_12', 'ema_26',
            'bb_upper', 'bb_middle', 'bb_lower', 'volume_ratio', 'css_value']
    df = pd.DataFrame(rows, columns=cols)
    for c in df.columns:
        if c != 'timestamp':
            df[c] = df[c].astype(float)
    return df, False


def _fetch_via_ccxt(symbol, timeframe='1h', limit=500):
    """Fetch fresh OHLCV via ccxt and compute indicators."""
    import ccxt
    exchange = ccxt.bybit({'enableRateLimit': True})
    base = symbol.replace('USDT', '')
    ccxt_symbol = f"{base}/USDT:USDT"
    ohlcv = exchange.fetch_ohlcv(ccxt_symbol, timeframe, limit=limit)
    if not ohlcv or len(ohlcv) < 50:
        return None
    
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = df[c].astype(float)
    return df


def _compute_indicators(df):
    """Compute all indicators on a raw OHLCV DataFrame."""
    import pandas as pd
    
    c = df['close']
    h = df['high']
    l = df['low']
    
    df['rsi_14'] = _rsi(c, 14)
    ema12 = _ema(c, 12)
    ema26 = _ema(c, 26)
    df['ema_12'] = ema12
    df['ema_26'] = ema26
    df['macd'] = ema12 - ema26
    df['macd_signal'] = _ema(df['macd'], 9)
    df['macd_hist'] = df['macd'] - df['macd_signal']
    df['atr_14'] = _atr(h, l, c, 14)
    df['sma_20'] = c.rolling(20).mean()
    df['sma_50'] = c.rolling(50).mean()
    df['sma_200'] = c.rolling(200).mean()
    bb_u, bb_m, bb_l = _bollinger(c)
    df['bb_upper'] = bb_u
    df['bb_middle'] = bb_m
    df['bb_lower'] = bb_l
    vol_sma = df['volume'].rolling(20).mean()
    df['volume_ratio'] = df['volume'] / vol_sma.replace(0, np.nan)
    df['adx'] = _adx(h, l, c, 14)
    psar = _psar(h, l)
    df['psar'] = psar
    df['psar_trend'] = np.where(psar < c, 'BULLISH', 'BEARISH')
    
    return df


# ─── Regime classification ─────────────────────────────────────────────

def _classify_regime(df, regime_config):
    """Rule-based regime classification. Returns (regime_name, confidence, details)."""
    last = df.iloc[-1]
    close = float(last['close'])
    
    # Derived features
    rsi = float(last.get('rsi_14', 50))
    macd_hist = float(last.get('macd_hist', 0))
    atr = float(last.get('atr_14', 0))
    atr_pct = atr / close * 100 if close else 0
    vol_ratio = float(last.get('volume_ratio', 1))
    sma50 = float(last.get('sma_50', close))
    sma200 = float(last.get('sma_200', close))
    sma20 = float(last.get('sma_20', close))
    bb_upper = float(last.get('bb_upper', close))
    bb_lower = float(last.get('bb_lower', close))
    bb_middle = float(last.get('bb_middle', close))
    bb_width = (bb_upper - bb_lower) / bb_middle * 100 if bb_middle else 0
    adx = float(last.get('adx', 20))
    price_vs_sma50 = (close - sma50) / sma50 * 100 if sma50 else 0
    sma_cross = (sma20 - sma200) / sma200 * 100 if sma200 else 0
    
    # 72h price change
    if len(df) >= 72:
        price_72h_ago = float(df.iloc[-72]['close'])
        price_change_72h = (close - price_72h_ago) / price_72h_ago * 100
    elif len(df) >= 24:
        price_24h_ago = float(df.iloc[-24]['close'])
        price_change_72h = (close - price_24h_ago) / price_24h_ago * 100
    else:
        price_change_72h = 0
    
    indicators = {
        'rsi_14': rsi,
        'macd_hist': macd_hist,
        'atr_pct': round(atr_pct, 2),
        'volume_ratio': round(vol_ratio, 2),
        'price_vs_sma50': round(price_vs_sma50, 2),
        'sma_cross': round(sma_cross, 2),
        'bb_width': round(bb_width, 2),
        'adx': round(adx, 1),
        'price_change_72h': round(price_change_72h, 2),
        'close': close,
    }
    
    # Score each regime
    scores = {}
    thresholds = regime_config.get('regimes', {})
    
    # PANIC_CRASH
    panic_score = 0
    if price_change_72h < -8:
        panic_score += 3
    elif price_change_72h < -5:
        panic_score += 1.5
    if rsi < 30:
        panic_score += 2
    elif rsi < 35:
        panic_score += 1
    if vol_ratio > 3:
        panic_score += 2
    elif vol_ratio > 2:
        panic_score += 1
    if macd_hist < 0:
        panic_score += 1
    if atr_pct > 4:
        panic_score += 1.5
    scores['PANIC_CRASH'] = panic_score
    
    # BEAR_TREND
    bear_score = 0
    if rsi < 42:
        bear_score += 2
    if macd_hist < 0:
        bear_score += 2
    if price_vs_sma50 < -1:
        bear_score += 2
    elif price_vs_sma50 < 0:
        bear_score += 1
    if sma_cross < -1:
        bear_score += 1.5
    if adx > 25:
        bear_score += 1
    if 30 <= rsi <= 42:
        bear_score += 0.5  # not extreme oversold, just bearish
    scores['BEAR_TREND'] = bear_score
    
    # RANGING
    range_score = 0
    if 40 <= rsi <= 60:
        range_score += 2
    if adx < 20:  # was 25 — too generous
        range_score += 2
    elif adx < 25:
        range_score += 1
    if bb_width < 3:
        range_score += 2
    elif bb_width < 5:
        range_score += 1
    if abs(price_vs_sma50) < 2:
        range_score += 1
    if vol_ratio < 1.5:
        range_score += 1
    # Penalty: extreme RSI contradicts ranging
    if rsi < 30 or rsi > 70:
        range_score -= 2
    scores['RANGING'] = range_score
    
    # BULL_TREND
    bull_score = 0
    if rsi > 55:
        bull_score += 2
    if macd_hist > 0:
        bull_score += 2
    if price_vs_sma50 > 1:
        bull_score += 2
    elif price_vs_sma50 > 0:
        bull_score += 1
    if sma_cross > 1:
        bull_score += 1.5
    if adx > 25:
        bull_score += 1
    scores['BULL_TREND'] = bull_score
    
    # EUPHORIA_PUMP
    eup_score = 0
    if price_change_72h > 10:
        eup_score += 3
    elif price_change_72h > 6:
        eup_score += 1.5
    if rsi > 70:
        eup_score += 2
    elif rsi > 65:
        eup_score += 1
    if vol_ratio > 3:
        eup_score += 2
    elif vol_ratio > 2:
        eup_score += 1
    if atr_pct > 3:
        eup_score += 1
    scores['EUPHORIA_PUMP'] = eup_score
    
    # Sort by score
    sorted_regimes = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_regime, best_score = sorted_regimes[0]
    second_regime, second_score = sorted_regimes[1]
    
    # Confidence: gap between best and second / max possible
    max_possible = 10  # rough max
    if best_score < 2:
        regime = 'TRANSITION'
        confidence = 0.4
    elif best_score - second_score < 1.5:
        regime = best_regime
        confidence = 0.5 + (best_score - second_score) / max_possible * 0.3
    else:
        regime = best_regime
        confidence = min(0.95, 0.6 + best_score / max_possible * 0.35)
    
    return regime, round(confidence, 2), indicators, scores


# ─── Regime-aware weights ──────────────────────────────────────────────

def _compute_dynamic_weights(df, regime, regime_config):
    """Compute correlation-based indicator weights for the given regime."""
    import pandas as pd
    
    if len(df) < 50:
        return regime_config.get('regimes', {}).get(regime, {}).get('default_weights', {})
    
    # Compute forward return (next candle)
    df = df.copy()
    df['fwd_return'] = df['close'].shift(-1) / df['close'] - 1
    df = df.dropna(subset=['fwd_return', 'rsi_14', 'macd_hist', 'atr_14',
                            'volume_ratio', 'sma_50', 'sma_200', 'bb_middle',
                            'bb_upper', 'bb_lower'])
    
    if len(df) < 30:
        return regime_config.get('regimes', {}).get(regime, {}).get('default_weights', {})
    
    # Features for correlation
    close = df['close']
    features = pd.DataFrame({
        'rsi_14': df['rsi_14'],
        'macd_hist': df['macd_hist'],
        'volume_ratio': df['volume_ratio'],
        'atr_pct': df['atr_14'] / close * 100,
        'price_vs_sma50': (close - df['sma_50']) / df['sma_50'] * 100,
        'bb_width': (df['bb_upper'] - df['bb_lower']) / df['bb_middle'] * 100,
        'sma_cross': (df['sma_20'] if 'sma_20' in df else df['sma_50']) 
                     - df['sma_200'],
    })
    
    # Replace inf/nan
    features = features.replace([np.inf, -np.inf], np.nan).fillna(0)
    
    # Correlation with forward return
    correlations = {}
    for col in features.columns:
        try:
            corr = abs(features[col].corr(df['fwd_return']))
            if np.isnan(corr):
                corr = 0.01
            correlations[col] = max(0.01, corr)
        except Exception:
            correlations[col] = 0.01
    
    # Normalize to sum=1
    total = sum(correlations.values())
    weights = {k: round(v / total, 3) for k, v in correlations.items()}
    
    return weights


# ─── Fear & Greed ──────────────────────────────────────────────────────

def _fetch_fng():
    """Fetch Fear & Greed Index from alternative.me."""
    try:
        import urllib.request, json as _json
        req = urllib.request.Request('https://api.alternative.me/fng/?limit=1',
                                     headers={'User-Agent': 'CryptoTrader/1.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = _json.loads(r.read())
            entry = data.get('data', [{}])[0]
            return int(entry.get('value', 50)), entry.get('value_classification', 'Neutral')
    except Exception:
        return None, None


# ─── Main detection function ───────────────────────────────────────────

def detect_regime(symbol='BTCUSDT', regime_config=None):
    """Detect current market regime for a symbol.
    
    Returns dict with regime, confidence, indicators, weights, etc.
    """
    import pandas as pd
    
    if regime_config is None:
        if REGIMES_FILE.exists():
            regime_config = json.loads(REGIMES_FILE.read_text())
        else:
            regime_config = {'regimes': {}}
    
    # Fetch data
    df, is_raw = _fetch_from_db(symbol, '1h', 500)
    # Staleness check: if DB data is >2h old, fetch fresh from Bybit
    if df is not None and len(df) > 0:
        try:
            last_ts = df['timestamp'].max()
            if hasattr(last_ts, 'timestamp'):
                age_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
            else:
                age_hours = 0
            if age_hours > 2:
                print(f"[regime] DB data {age_hours:.0f}h stale, fetching from Bybit", file=sys.stderr)
                df = _fetch_via_ccxt(symbol, '1h', 500)
                is_raw = True
        except Exception:
            pass
    if df is None or len(df) < 30:
        # Fallback to ccxt
        df = _fetch_via_ccxt(symbol, '1h', 500)
        is_raw = True
        if df is None:
            return {'regime': 'UNKNOWN', 'confidence': 0, 'error': 'No data available'}
    
    # Compute indicators if needed
    if is_raw or 'rsi_14' not in df.columns:
        df = _compute_indicators(df)
    
    # Drop NaN rows from indicator warmup
    df = df.dropna(subset=['rsi_14', 'macd_hist']).reset_index(drop=True)
    if len(df) < 20:
        return {'regime': 'UNKNOWN', 'confidence': 0, 'error': 'Insufficient data after indicator computation'}
    
    # Classify regime
    regime, confidence, indicators, scores = _classify_regime(df, regime_config)
    
    # Compute weights
    weights = _compute_dynamic_weights(df, regime, regime_config)
    
    # Estimate regime duration (how many candles back has the same regime dominated)
    duration_hours = 0
    for i in range(len(df) - 1, max(0, len(df) - 200), -1):
        window = df.iloc[max(0, i-23):i+1]
        r, _, _, _ = _classify_regime(window, regime_config)
        if r == regime:
            duration_hours += 1
        else:
            break
    
    # FNG
    fng_value, fng_label = _fetch_fng()
    
    result = {
        'regime': regime,
        'confidence': confidence,
        'indicators': indicators,
        'regime_scores': {k: round(v, 1) for k, v in scores.items()},
        'weights': weights,
        'regime_duration_hours': duration_hours,
        'fng': {'value': fng_value, 'label': fng_label},
        'symbol': symbol,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    
    # Add action bias from config
    regime_def = regime_config.get('regimes', {}).get(regime, {})
    result['action_bias'] = regime_def.get('action_bias', 'neutral')
    result['description'] = regime_def.get('description', '')
    
    # Save dynamic weights
    try:
        regime_config['dynamic_weights'] = regime_config.get('dynamic_weights', {})
        regime_config['dynamic_weights'][regime] = weights
        regime_config['last_updated'] = datetime.now(timezone.utc).isoformat()
        REGIMES_FILE.write_text(json.dumps(regime_config, indent=2))
    except Exception:
        pass
    
    return result


# ─── CLI ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Market regime detector')
    parser.add_argument('--symbol', default='BTCUSDT', help='Symbol to analyze')
    parser.add_argument('--json', action='store_true', help='Output JSON only')
    args = parser.parse_args()
    
    result = detect_regime(args.symbol)
    
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*50}")
        print(f"  REGIME: {result['regime']}  (confidence: {result['confidence']:.0%})")
        print(f"{'='*50}")
        if result.get('error'):
            print(f"  ERROR: {result['error']}")
            return
        
        ind = result['indicators']
        print(f"\n  Symbol: {result['symbol']}")
        print(f"  Price: ${ind['close']:,.2f}")
        print(f"  RSI(14): {ind['rsi_14']:.1f}")
        print(f"  MACD hist: {ind['macd_hist']:.2f}")
        print(f"  ADX: {ind['adx']:.1f}")
        print(f"  ATR%: {ind['atr_pct']:.2f}%")
        print(f"  Vol ratio: {ind['volume_ratio']:.2f}x")
        print(f"  Price vs SMA50: {ind['price_vs_sma50']:+.2f}%")
        print(f"  BB width: {ind['bb_width']:.2f}%")
        print(f"  72h change: {ind['price_change_72h']:+.2f}%")
        
        if result.get('fng', {}).get('value'):
            print(f"  Fear&Greed: {result['fng']['value']} ({result['fng']['label']})")
        
        print(f"\n  Duration: ~{result['regime_duration_hours']}h")
        print(f"  Action bias: {result['action_bias']}")
        
        print(f"\n  Regime scores:")
        for r, s in sorted(result['regime_scores'].items(), key=lambda x: -x[1]):
            bar = '█' * int(s) + '░' * (10 - int(s))
            print(f"    {r:15s} {bar} {s:.1f}")
        
        print(f"\n  Indicator weights:")
        for k, w in sorted(result['weights'].items(), key=lambda x: -x[1]):
            print(f"    {k:20s} {w:.3f}")
        
        print()


if __name__ == '__main__':
    main()
