#!/usr/bin/env python
"""Backtest of a CONSOLIDATED strategy researched from public sources (2025-26):
trend-following with an ADX filter (the single biggest lever for positive expectancy:
ADX>threshold skips chop), EMA9/21 crossover entries (only on a FRESH cross -> few trades),
ATR-based stop, fixed R/R take-profit and an optional ATR trailing stop.

Indicators computed vectorised (fast). Compares against a baseline 'always EMA-trend'.

Usage: python tools/backtest_v3.py [--days 30] [--symbols BTCUSDT ETHUSDT]
"""
import os, sys, argparse, itertools
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.core.config import Config
from src.core.database import DatabaseManager
from src.core.logger import setup_logger

TAKER_FEE = 0.00055
MAX_HOLD = 96  # 5m bars (~8h)


def load_5m(db, symbol, days):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with db.get_session() as s:
        rows = s.execute(
            text("SELECT timestamp, open, high, low, close, volume FROM ohlcv_raw "
                 "WHERE symbol=:sym AND timeframe='1m' AND timestamp >= :since ORDER BY timestamp"),
            {"sym": symbol, "since": since},
        ).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").astype(float)
    o = df.resample("5min").agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "volume": "sum"}).dropna().reset_index()
    return o


def rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def add_indicators(df):
    h, l, c = df["high"], df["low"], df["close"]
    df["ema9"] = c.ewm(span=9, adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()
    # ATR (Wilder)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["atr"] = rma(tr, 14)
    # ADX (Wilder)
    up = h.diff()
    dn = -l.diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    atr_d = rma(tr, 14)
    plus_di = 100 * rma(plus_dm, 14) / atr_d
    minus_di = 100 * rma(minus_dm, 14) / atr_d
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = rma(dx.fillna(0), 14)
    # RSI (for guards)
    d = c.diff()
    df["rsi"] = 100 - 100 / (1 + rma(d.clip(lower=0), 14) / rma(-d.clip(upper=0), 14))
    # volume ratio
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean()
    return df


def simulate(df, p):
    n = len(df)
    e9, e21, adx, atr = df["ema9"].values, df["ema21"].values, df["adx"].values, df["atr"].values
    rsi, vr = df["rsi"].values, df["vol_ratio"].values
    op, hi, lo, cl = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    trades = []
    i = 30
    while i < n - 1:
        cross_up = e9[i - 1] <= e21[i - 1] and e9[i] > e21[i]
        cross_dn = e9[i - 1] >= e21[i - 1] and e9[i] < e21[i]
        long = short = False
        if adx[i] >= p["adx"] and (vr[i] or 0) >= p["vol_gate"]:
            if cross_up and cl[i] > e21[i] and rsi[i] < 78:
                long = True
            elif cross_dn and cl[i] < e21[i] and rsi[i] > 22:
                short = True
        if not (long or short):
            i += 1; continue
        entry = op[i + 1]
        a = atr[i]
        if not (a > 0):
            i += 1; continue
        sl_dist = p["sl_atr"] * a
        tp_dist = p["rr"] * sl_dist
        sl_price = entry - sl_dist if long else entry + sl_dist
        tp_price = entry + tp_dist if long else entry - tp_dist
        best = entry
        exit_px, j = None, i + 1
        for j in range(i + 1, min(i + 1 + MAX_HOLD, n)):
            if long:
                if lo[j] <= sl_price: exit_px = sl_price; break
                if hi[j] >= tp_price: exit_px = tp_price; break
                if p["trail"]:
                    best = max(best, hi[j])
                    if best - entry >= sl_dist:  # in profit by 1R -> trail
                        sl_price = max(sl_price, best - p["trail"] * a)
            else:
                if hi[j] >= sl_price: exit_px = sl_price; break
                if lo[j] <= tp_price: exit_px = tp_price; break
                if p["trail"]:
                    best = min(best, lo[j])
                    if entry - best >= sl_dist:
                        sl_price = min(sl_price, best + p["trail"] * a)
        if exit_px is None:
            exit_px = cl[j]
        gross = (exit_px - entry) / entry if long else (entry - exit_px) / entry
        trades.append(gross - 2 * TAKER_FEE)
        i = j + 1
    if not trades:
        return {"trades": 0, "winrate": 0, "total": 0}
    wins = [t for t in trades if t > 0]
    return {"trades": len(trades), "winrate": len(wins) / len(trades), "total": sum(trades)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    args = ap.parse_args()
    config = Config.load(None)
    db = DatabaseManager(config.postgresql, setup_logger("bt3", level="ERROR"))

    data = {}
    for sym in args.symbols:
        df = load_5m(db, sym, args.days)
        if df is None or len(df) < 100:
            print(f"{sym}: insufficient data"); continue
        data[sym] = add_indicators(df)
        print(f"{sym}: {len(df)} 5m bars ({df['ts'].iloc[0].date()}..{df['ts'].iloc[-1].date()})")

    grid = {"adx": [20, 25, 30], "sl_atr": [1.5, 2.0], "rr": [2.0, 3.0],
            "vol_gate": [0.0, 0.8], "trail": [0.0, 1.0, 2.0]}
    keys = list(grid)
    combos = [dict(zip(keys, v)) for v in itertools.product(*grid.values())]
    results = []
    for p in combos:
        agg = {"trades": 0, "wins": 0.0, "total": 0.0}
        for sym, df in data.items():
            r = simulate(df, p)
            agg["trades"] += r["trades"]; agg["wins"] += r["winrate"] * r["trades"]; agg["total"] += r["total"]
        if agg["trades"] < 5:
            continue
        results.append({**p, "trades": agg["trades"], "winrate": agg["wins"] / agg["trades"],
                        "total_pct": agg["total"] * 100})
    results.sort(key=lambda r: r["total_pct"], reverse=True)
    print(f"\n{'='*92}\nADX-trend (EMA9/21 cross + ADX filter + ATR R/R + trail) — {args.days}d, fee {2*TAKER_FEE*100:.2f}%/trade:\n")
    hdr = f"{'adx':>5}{'sl_atr':>7}{'rr':>5}{'vgate':>6}{'trail':>6}{'trades':>7}{'win%':>7}{'tot%':>8}"
    print(hdr); print("-" * len(hdr))
    for r in results[:15]:
        print(f"{r['adx']:>5.0f}{r['sl_atr']:>7.1f}{r['rr']:>5.1f}{r['vol_gate']:>6.1f}{r['trail']:>6.1f}"
              f"{r['trades']:>7d}{r['winrate']*100:>7.1f}{r['total_pct']:>8.2f}")
    if results:
        b = results[0]
        print(f"\nBEST: adx>{b['adx']:.0f} sl{b['sl_atr']}xATR rr{b['rr']} vg{b['vol_gate']} trail{b['trail']} "
              f"-> {b['trades']} trades, {b['winrate']*100:.0f}% win, {b['total_pct']:+.2f}%")


if __name__ == "__main__":
    main()
