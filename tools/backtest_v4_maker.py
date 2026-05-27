#!/usr/bin/env python
"""FILL-AWARE maker backtest for ADX-trend. The optimistic v3 maker run assumed every
order fills at next-bar open; that is unrealistic for limit/maker orders on a trend entry
(price often runs away). Here:

  ENTRY: maker limit at the signal bar's close. Fills ONLY if, within `fill_window` bars,
         price trades back through the limit (low<=limit for BUY / high>=limit for SELL);
         otherwise the trade is MISSED (trend ran away). Entry fee = MAKER.
  EXIT:  TP as a maker limit (fee MAKER); SL as a stop-market (fee TAKER); time-exit TAKER.

Reports fill-rate and net per symbol on cached 90d data (/tmp/adxval_*.pkl from validate_adx.py).

Usage: python tools/backtest_v4_maker.py [--symbols ...] [--fill-window 3]
"""
import os, sys, argparse, pickle, itertools
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_v3 import add_indicators

MAKER_FEE = 0.0002
TAKER_FEE = 0.00055
MAX_HOLD = 96


def simulate_maker(df, p):
    e9, e21, adx, atr = df["ema9"].values, df["ema21"].values, df["adx"].values, df["atr"].values
    rsi, vr = df["rsi"].values, df["vol_ratio"].values
    op, hi, lo, cl = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(df); fw = p["fill_window"]
    trades, signals, missed = [], 0, 0
    i = 30
    while i < n - 1:
        cu = e9[i-1] <= e21[i-1] and e9[i] > e21[i]
        cd = e9[i-1] >= e21[i-1] and e9[i] < e21[i]
        long = short = False
        if adx[i] >= p["adx"] and (vr[i] or 0) >= p["vol_gate"]:
            if cu and cl[i] > e21[i] and rsi[i] < 78: long = True
            elif cd and cl[i] < e21[i] and rsi[i] > 22: short = True
        if not (long or short):
            i += 1; continue
        signals += 1
        limit = cl[i]; a = atr[i]
        if not (a > 0): i += 1; continue
        # try to fill the maker limit within fill_window bars
        eb = None
        for j in range(i + 1, min(i + 1 + fw, n)):
            if long and lo[j] <= limit: eb = j; break
            if short and hi[j] >= limit: eb = j; break
        if eb is None:
            missed += 1; i += 1; continue   # trend ran away, no fill
        entry = limit
        sl_dist = p["sl_atr"] * a; tp_dist = p["rr"] * sl_dist
        sl_price = entry - sl_dist if long else entry + sl_dist
        tp_price = entry + tp_dist if long else entry - tp_dist
        exit_px, exit_fee, j = None, TAKER_FEE, eb
        for j in range(eb + 1, min(eb + 1 + MAX_HOLD, n)):
            if long:
                if lo[j] <= sl_price: exit_px, exit_fee = sl_price, TAKER_FEE; break
                if hi[j] >= tp_price: exit_px, exit_fee = tp_price, MAKER_FEE; break
            else:
                if hi[j] >= sl_price: exit_px, exit_fee = sl_price, TAKER_FEE; break
                if lo[j] <= tp_price: exit_px, exit_fee = tp_price, MAKER_FEE; break
        if exit_px is None:
            exit_px, exit_fee = cl[j], TAKER_FEE
        gross = (exit_px - entry) / entry if long else (entry - exit_px) / entry
        trades.append(gross - MAKER_FEE - exit_fee)  # entry maker + exit (maker/taker)
        i = j + 1
    if not trades:
        return {"trades": 0, "winrate": 0, "total": 0, "signals": signals, "fill": 0}
    wins = [t for t in trades if t > 0]
    return {"trades": len(trades), "winrate": len(wins)/len(trades), "total": sum(trades),
            "signals": signals, "fill": len(trades)/signals if signals else 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--fill-window", type=int, default=3)
    args = ap.parse_args()
    data = {}
    for sym in args.symbols:
        c = f"/tmp/adxval_{sym}_{args.days}d.pkl"
        if not os.path.exists(c):
            print(f"{sym}: no cache ({c}) — run validate_adx.py first"); continue
        data[sym] = add_indicators(pickle.load(open(c, "rb")))

    configs = [
        {"adx": 25, "sl_atr": 2.0, "rr": 3.0, "vol_gate": 0.8, "fill_window": args.fill_window},
        {"adx": 20, "sl_atr": 2.0, "rr": 3.0, "vol_gate": 0.8, "fill_window": args.fill_window},
        {"adx": 25, "sl_atr": 2.0, "rr": 2.0, "vol_gate": 0.8, "fill_window": args.fill_window},
        {"adx": 30, "sl_atr": 2.0, "rr": 3.0, "vol_gate": 0.8, "fill_window": args.fill_window},
    ]
    print(f"FILL-AWARE maker (entry maker, TP maker, SL taker), fill_window={args.fill_window} bars\n")
    for p in configs:
        print(f"--- adx{p['adx']} sl{p['sl_atr']} rr{p['rr']} vg{p['vol_gate']} ---")
        agg_t = agg_tot = agg_w = 0.0
        for sym, df in data.items():
            r = simulate_maker(df, p)
            print(f"  {sym:9} {r['trades']:>3}/{r['signals']:<3} filled({r['fill']*100:>3.0f}%) "
                  f"win {r['winrate']*100:>4.0f}%  net {r['total']*100:>+6.2f}%")
            agg_t += r["trades"]; agg_tot += r["total"]; agg_w += r["winrate"]*r["trades"]
        if agg_t:
            print(f"  {'AGG':9} {int(agg_t):>3}      win {agg_w/agg_t*100:>4.0f}%  net {agg_tot*100:>+6.2f}%\n")


if __name__ == "__main__":
    main()
