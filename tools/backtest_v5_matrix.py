#!/usr/bin/env python
"""Matrix experiment: ADX-trend (fill-aware maker) across SYMBOL x TIMEFRAME.
Tests two ideas at once:
  (1) higher timeframe (15m/1h) -> fewer trades -> less fee drag
  (2) less-efficient alt pairs  -> TA edge may survive
Reuses add_indicators (backtest_v3) + the fill-aware maker model (entry maker, TP maker,
SL taker). 5m base fetched from Bybit (cached); higher TFs resampled from it.

Usage: python tools/backtest_v5_matrix.py
"""
import os, sys, time, pickle
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_v3 import add_indicators
from backtest_v4_maker import simulate_maker

DAYS = 90
CONFIG = {"adx": 25, "sl_atr": 2.0, "rr": 3.0, "vol_gate": 0.8, "fill_window": 3}
LIQUID = ["BTCUSDT", "ETHUSDT"]
ALTS = ["SOLUSDT", "DOGEUSDT", "XRPUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT"]
TFS = {"5m": None, "15m": "15min", "1h": "60min"}


def fetch_5m(symbol, days=DAYS):
    cache = f"/tmp/adxval_{symbol}_{days}d.pkl"
    if os.path.exists(cache):
        return pickle.load(open(cache, "rb"))
    for k in ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy'):
        os.environ.pop(k, None)
    import ccxt
    ex = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'linear'}})
    mk = symbol.replace('USDT', '/USDT:USDT')
    since = ex.parse8601((pd.Timestamp.now('UTC') - pd.Timedelta(days=days)).isoformat())
    now = ex.milliseconds(); out = []
    while since < now:
        b = None
        for att in range(5):
            try:
                b = ex.fetch_ohlcv(mk, '5m', since=since, limit=1000); break
            except Exception as e:
                if 'Rate' in type(e).__name__ or '10006' in str(e):
                    time.sleep(1.5 * (att + 1)); continue
                raise
        if not b: break
        out += b; since = b[-1][0] + 300000
        if len(b) < 1000: break
        time.sleep(0.4)
    if not out: return None
    df = pd.DataFrame(out, columns=['ts', 'open', 'high', 'low', 'close', 'volume']).drop_duplicates('ts')
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df = df.reset_index(drop=True)
    pickle.dump(df, open(cache, "wb"))
    return df


def resample(df5, rule):
    if rule is None:
        return df5
    o = df5.set_index('ts').resample(rule).agg({'open': 'first', 'high': 'max', 'low': 'min',
                                                 'close': 'last', 'volume': 'sum'}).dropna().reset_index()
    return o


def main():
    print(f"ADX-trend fill-aware maker, {DAYS}d, config {CONFIG}\n")
    print(f"{'symbol':10}{'tf':>5}{'trades':>8}{'win%':>7}{'net%':>9}")
    print("-" * 39)
    rows = []
    for sym in LIQUID + ALTS:
        df5 = fetch_5m(sym)
        if df5 is None or len(df5) < 500:
            print(f"{sym:10} fetch failed"); continue
        for tf, rule in TFS.items():
            df = add_indicators(resample(df5, rule))
            r = simulate_maker(df, CONFIG)
            rows.append((sym, tf, r))
            print(f"{sym:10}{tf:>5}{r['trades']:>8}{r['winrate']*100:>7.0f}{r['total']*100:>+9.2f}")
        print()
    # summaries per timeframe (aggregate net)
    print("=== aggregate net% by timeframe ===")
    for tf in TFS:
        sub = [r for s, t, r in rows if t == tf]
        liq = [r for s, t, r in rows if t == tf and s in LIQUID]
        alt = [r for s, t, r in rows if t == tf and s in ALTS]
        print(f"  {tf:>4}: ALL {sum(r['total'] for r in sub)*100:>+7.2f}% | "
              f"BTC/ETH {sum(r['total'] for r in liq)*100:>+7.2f}% | "
              f"alts {sum(r['total'] for r in alt)*100:>+7.2f}%")


if __name__ == "__main__":
    main()
