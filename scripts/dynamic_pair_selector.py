#!/usr/bin/env python3
"""Dynamic pair selector for CryptoTrader.
Scans Bybit USDT linear perpetuals for volume spikes + price momentum.
Returns top N pairs sorted by combined score.

Usage:
    python scripts/dynamic_pair_selector.py [--top 7] [--min-vol-usd 1000000]
"""
import json
import sys
import time
import re
from datetime import datetime, timezone

import ccxt
import psycopg2

DB_CONFIG = dict(host='192.168.0.149', port=5432, database='cryptotrader',
                 user='cryptotrader', password='cryptotrader123')

# Patterns to exclude
_EXCLUDE_PATTERNS = [
    r'.*USDC$', r'.*BUSD$', r'.*TUSD$', r'.*DAI$', r'.*USD1$',
    r'.*3L$', r'.*3S$', r'.*5L$', r'.*5S$',
    r'.*DOWN$', r'.*UP$',
    r'^EUR', r'^GBP', r'^AUD', r'^TRY',
    r'.*币.*',
]
_EXCLUDE_RE = [re.compile(p) for _p in _EXCLUDE_PATTERNS for p in [_p]]

# Always include these regardless of score
_ALWAYS_INCLUDE = ['BTCUSDT', 'ETHUSDT']


def _is_excluded(symbol: str) -> bool:
    return any(rx.match(symbol) for rx in _EXCLUDE_RE)


def _fetch_bybit_tickers(exchange: ccxt.bybit) -> dict:
    """Fetch all USDT linear perpetual tickers from Bybit."""
    tickers = exchange.fetch_tickers(params={'category': 'linear'})
    result = {}
    for sym, t in tickers.items():
        if not sym.endswith('/USDT:USDT'):
            continue
        base = sym.split('/')[0]
        symbol_usdt = f"{base}USDT"
        vol_usd = (t.get('quoteVolume') or 0)
        result[symbol_usdt] = {
            'symbol': symbol_usdt,
            'ccxt_symbol': sym,
            'last': t.get('last', 0),
            'volume_24h_usd': vol_usd,
            'change_pct': t.get('percentage', 0) or 0,
            'high_24h': t.get('high', 0),
            'low_24h': t.get('low', 0),
        }
    return result


def _compute_4h_momentum(exchange: ccxt.bybit, ccxt_symbol: str) -> dict:
    """Get 4h price change and volume spike for a symbol."""
    try:
        ohlcv = exchange.fetch_ohlcv(ccxt_symbol, '1h', limit=24)
        if len(ohlcv) < 24:
            return {'price_change_4h_pct': 0, 'volume_spike': 0}
        # Last 4 candles = 4h
        recent_4h = ohlcv[-4:]
        prior_20h = ohlcv[-24:-4]
        close_now = recent_4h[-1][4]
        close_4h_ago = recent_4h[0][1]  # open of first candle in window
        price_change = (close_now - close_4h_ago) / close_4h_ago * 100 if close_4h_ago else 0
        vol_recent_4h = sum(c[5] for c in recent_4h)
        vol_prior_20h = sum(c[5] for c in prior_20h)
        vol_avg_4h = vol_prior_20h / 5 if vol_prior_20h else 1  # avg 4h volume from prior 20h
        volume_spike = vol_recent_4h / vol_avg_4h if vol_avg_4h else 0
        return {
            'price_change_4h_pct': round(price_change, 2),
            'volume_spike': round(volume_spike, 2),
        }
    except Exception:
        return {'price_change_4h_pct': 0, 'volume_spike': 0}


def _get_db_symbols_with_data() -> set:
    """Get symbols that have recent 1h data in ohlcv_raw."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT symbol FROM ohlcv_raw
            WHERE exchange = 'bybit' AND timeframe = '1h'
              AND timestamp > NOW() - INTERVAL '48 hours'
        """)
        syms = {r[0] for r in cur.fetchall()}
        cur.close()
        conn.close()
        return syms
    except Exception:
        return set()


def select_pairs(top_n: int = 7, min_vol_usd: float = 1_000_000,
                 verbose: bool = False) -> list:
    """Select top trading pairs by volume spike + price momentum.
    
    Returns list of dicts sorted by combined score (highest first).
    BTCUSDT and ETHUSDT are always included.
    """
    exchange = ccxt.bybit({'enableRateLimit': True})
    
    if verbose:
        print("[pair_selector] Fetching Bybit tickers...", file=sys.stderr)
    tickers = _fetch_bybit_tickers(exchange)
    if verbose:
        print(f"[pair_selector] Got {len(tickers)} USDT:USDT tickers", file=sys.stderr)
    
    # Filter by min volume and exclude patterns
    candidates = {}
    for sym, t in tickers.items():
        if _is_excluded(sym):
            continue
        if t['volume_24h_usd'] < min_vol_usd:
            continue
        candidates[sym] = t
    
    if verbose:
        print(f"[pair_selector] {len(candidates)} after volume/exclusion filter", file=sys.stderr)
    
    # Always include anchors
    anchors = []
    for a in _ALWAYS_INCLUDE:
        if a in candidates:
            anchors.append(a)
    
    # Score by ticker data directly (FAST — no per-symbol API calls)
    # Use 24h change and volume as proxies for momentum
    scored = []
    for sym, t in sorted(candidates.items(), key=lambda x: x[1]['volume_24h_usd'], reverse=True)[:30]:
        if sym in _ALWAYS_INCLUDE:
            continue
        # Score: 60% volume rank + 40% abs(24h change)
        vol_score = min(t['volume_24h_usd'] / 100_000_000, 10)  # normalize, cap at 10
        change_score = abs(t['change_pct'])
        score = 0.4 * vol_score + 0.6 * change_score
        scored.append({
            'symbol': sym,
            'price': t['last'],
            'volume_24h_usd': round(t['volume_24h_usd']),
            'change_24h_pct': round(t['change_pct'], 2),
            'price_change_4h_pct': round(t['change_pct'], 2),  # proxy: use 24h change
            'volume_spike': round(vol_score, 2),
            'score': round(score, 2),
        })
    
    scored.sort(key=lambda x: x['score'], reverse=True)
    
    # Select top N (excluding anchors from count)
    selected = []
    for s in scored:
        if s['symbol'] in _ALWAYS_INCLUDE:
            continue
        if len(selected) >= top_n - len(anchors):
            break
        if s['score'] >= 2.0 or abs(s['change_24h_pct']) >= 3.0:
            selected.append(s)
    
    # Prepend anchors
    for a in anchors:
        if a in candidates:
            t = candidates[a]
            selected.insert(0, {
                'symbol': a,
                'price': t['last'],
                'volume_24h_usd': round(t['volume_24h_usd']),
                'change_24h_pct': round(t['change_pct'], 2),
                'price_change_4h_pct': 0,
                'volume_spike': 0,
                'score': 0,  # always included
            })
    
    return selected


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Dynamic pair selector')
    parser.add_argument('--top', type=int, default=7, help='Number of pairs to select')
    parser.add_argument('--min-vol-usd', type=float, default=1_000_000, help='Min 24h volume in USD')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()
    
    pairs = select_pairs(top_n=args.top, min_vol_usd=args.min_vol_usd, verbose=args.verbose)
    
    print(json.dumps(pairs, indent=2))
    
    if args.verbose:
        print(f"\n[pair_selector] Selected {len(pairs)} pairs:", file=sys.stderr)
        for p in pairs:
            tag = "★" if p['symbol'] in _ALWAYS_INCLUDE else " "
            print(f"  {tag} {p['symbol']:15s} score={p['score']:6.2f} "
                  f"vol_spike={p['volume_spike']:5.1f}x "
                  f"4hΔ={p['price_change_4h_pct']:+6.2f}%",
                  file=sys.stderr)


if __name__ == '__main__':
    main()
