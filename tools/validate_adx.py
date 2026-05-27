#!/usr/bin/env python
"""Robustness validation of the ADX-trend strategy on LONGER history fetched live from
Bybit (the DB only holds ~18d). Walk-forward across consecutive windows + per-symbol +
a small grid, to check the +edge is not an 18-day fluke before changing live behaviour.

Usage: python tools/validate_adx.py [--days 90] [--symbols BTCUSDT ETHUSDT SOLUSDT] [--windows 6]
"""
import os, sys, argparse, time, itertools, pickle
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_v3
from backtest_v3 import add_indicators, simulate


def fetch_bybit_5m(symbol, days):
    for k in ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy'):
        os.environ.pop(k, None)
    import ccxt
    ex = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'linear'}})
    market = symbol.replace('USDT', '/USDT:USDT')
    ms = ex.parse8601((pd.Timestamp.utcnow() - pd.Timedelta(days=days)).isoformat())
    out, since = [], ms
    now = ex.milliseconds()
    while since < now:
        batch = None
        for attempt in range(5):
            try:
                batch = ex.fetch_ohlcv(market, '5m', since=since, limit=1000)
                break
            except Exception as e:
                if 'Rate' in type(e).__name__ or '10006' in str(e):
                    time.sleep(1.5 * (attempt + 1))  # backoff on rate limit
                    continue
                raise
        if not batch:
            break
        out += batch
        since = batch[-1][0] + 5 * 60 * 1000
        if len(batch) < 1000:
            break
        time.sleep(0.4)  # gentle throttle between pages
    if not out:
        return None
    df = pd.DataFrame(out, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('ts')
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    return df.reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--windows", type=int, default=6)
    ap.add_argument("--fee", type=float, default=None, help="per-side fee (overrides); 0.00055 taker, 0.0002 maker")
    args = ap.parse_args()
    if args.fee is not None:
        backtest_v3.TAKER_FEE = args.fee
    print(f"(per-side fee = {backtest_v3.TAKER_FEE})")

    P = {"adx": 25, "sl_atr": 2.0, "rr": 3.0, "vol_gate": 0.8, "trail": 0.0}
    data = {}
    for sym in args.symbols:
        cache = f"/tmp/adxval_{sym}_{args.days}d.pkl"
        if os.path.exists(cache):
            df = pickle.load(open(cache, "rb"))
        else:
            df = fetch_bybit_5m(sym, args.days)
            if df is not None:
                pickle.dump(df, open(cache, "wb"))
        if df is None or len(df) < 300:
            print(f"{sym}: fetch failed/short"); continue
        data[sym] = add_indicators(df)
        print(f"{sym}: {len(df)} 5m bars ({df['ts'].iloc[0].date()}..{df['ts'].iloc[-1].date()})")

    print(f"\n=== WALK-FORWARD ({args.windows} windows), config adx25/sl2.0/rr3.0/vg0.8 ===")
    agg_total = 0.0
    for sym, df in data.items():
        n = len(df); w = n // args.windows
        parts = []
        for k in range(args.windows):
            seg = df.iloc[k * w:(k + 1) * w].reset_index(drop=True)
            r = simulate(seg, P)
            parts.append(r['total'] * 100)
        full = simulate(df, P)
        agg_total += full['total'] * 100
        pos = sum(1 for x in parts if x > 0)
        print(f"{sym:9} FULL {full['total']*100:>+6.2f}% ({full['trades']}tr {full['winrate']*100:.0f}%) | "
              f"windows+: {pos}/{args.windows} | " + " ".join(f"{x:+.1f}" for x in parts))
    print(f"\nAggregate FULL total across symbols: {agg_total:+.2f}%")

    print(f"\n=== GRID on full {args.days}d (top 8 by total) ===")
    grid = {"adx": [20, 25, 30], "sl_atr": [1.5, 2.0], "rr": [2.0, 3.0], "vol_gate": [0.0, 0.8], "trail": [0.0, 1.5]}
    keys = list(grid)
    res = []
    for combo in itertools.product(*grid.values()):
        p = dict(zip(keys, combo))
        tot = tr = wsum = 0.0
        for sym, df in data.items():
            r = simulate(df, p); tot += r['total']; tr += r['trades']; wsum += r['winrate'] * r['trades']
        if tr >= 10:
            res.append((tot * 100, int(tr), wsum / tr * 100, p))
    res.sort(reverse=True)
    for tot, tr, win, p in res[:8]:
        print(f"  adx{p['adx']:>2} sl{p['sl_atr']} rr{p['rr']} vg{p['vol_gate']} trail{p['trail']} -> {tr:>4}tr {win:>4.0f}% {tot:>+7.2f}%")


if __name__ == "__main__":
    main()
