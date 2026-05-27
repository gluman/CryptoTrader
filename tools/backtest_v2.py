#!/usr/bin/env python
"""Strategy-research backtest. Tests ALTERNATIVE entry logics (not the 1:1 scalp that
the rule-based v1 backtest proved unprofitable). Reuses the project's calculate_indicators()
on a rolling 200-bar window, stores the FULL indicator dict per bar, then replays several
strategies with high R/R, regime filters and optional trailing stop.

Strategies:
  trend     - trend-following: trade with CSS/SMA trend, only when FDI regime='trending'
  breakout  - Bollinger breakout confirmed by volume
  meanrev   - selective counter-trend, only when regime='ranging', RSI extremes near bands

Usage: python tools/backtest_v2.py [--days 30] [--symbols BTCUSDT ETHUSDT]
"""
import os, sys, argparse, itertools
from datetime import datetime, timedelta, timezone
import pandas as pd
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.core.config import Config
from src.core.database import DatabaseManager
from src.core.logger import setup_logger
from src.agents import TradingDecisionAgent, SentimentAgent

TAKER_FEE = 0.00055
WINDOW = 200
MAX_HOLD = 96  # 5m bars (~8h) — trend trades need room


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
                                  "close": "last", "volume": "sum"}).dropna()
    return o.reset_index()


def precompute(agent, df5):
    out = []
    for i in range(WINDOW, len(df5)):
        window = df5.iloc[i - WINDOW:i + 1][["open", "high", "low", "close", "volume"]]
        ind = agent.calculate_indicators(window)
        out.append(ind if ind else None)
    return out


# --- strategy entry logic: returns 'BUY' | 'SELL' | None ---
def strat_trend(d):
    if d.get("regime") != "trending":
        return None
    px, sma50, mh = d["price"], d["sma_50"], d.get("macd_hist", 0)
    css = d.get("css_value", 0)
    if css > 0 and px > sma50 and mh > 0 and 40 <= d.get("rsi_14", 50) <= 68:
        return "BUY"
    if css < 0 and px < sma50 and mh < 0 and 32 <= d.get("rsi_14", 50) <= 60:
        return "SELL"
    return None


def strat_breakout(d):
    vr = d.get("volume_ratio", 0)
    if vr < 1.8:
        return None
    px = d["price"]
    if px > d["bb_upper"] and d.get("css_value", 0) > 0:
        return "BUY"
    if px < d["bb_lower"] and d.get("css_value", 0) < 0:
        return "SELL"
    return None


def strat_meanrev(d):
    if d.get("regime") != "ranging":
        return None
    rsi, px = d.get("rsi_14", 50), d["price"]
    if rsi < 25 and px <= d["bb_lower"] * 1.002:
        return "BUY"
    if rsi > 75 and px >= d["bb_upper"] * 0.998:
        return "SELL"
    return None


STRATS = {"trend": strat_trend, "breakout": strat_breakout, "meanrev": strat_meanrev}


def simulate(df5, inds, sfn, p):
    n = len(df5)
    trades = []
    i = 0
    while i < len(inds) - 1:
        d = inds[i]
        bar = i + WINDOW
        sig = sfn(d) if d else None
        if sig is None or d.get("volume_ratio", 0) < p["vol_gate"]:
            i += 1; continue
        if bar + 1 >= n:
            break
        entry = float(df5["open"].iloc[bar + 1])
        atr_pct = (d.get("atr_14", 0) / d["price"]) if d.get("price") else 0
        sl_pct = max(p["sl_floor"], p["sl_atr"] * atr_pct)
        tp_pct = sl_pct * p["rr"]  # fixed R/R
        long = sig == "BUY"
        sl_price = entry * (1 - sl_pct) if long else entry * (1 + sl_pct)
        tp_price = entry * (1 + tp_pct) if long else entry * (1 - tp_pct)
        trail_arm = p.get("trail", 0)  # arm trailing after this much favorable move (pct of entry); 0=off
        best = entry
        exit_px, j = None, bar + 1
        for j in range(bar + 1, min(bar + 1 + MAX_HOLD, n)):
            hi, lo = float(df5["high"].iloc[j]), float(df5["low"].iloc[j])
            if long:
                if lo <= sl_price: exit_px = sl_price; break
                if hi >= tp_price: exit_px = tp_price; break
                if trail_arm:
                    best = max(best, hi)
                    if best >= entry * (1 + trail_arm):
                        sl_price = max(sl_price, best * (1 - p["trail_dist"]))
            else:
                if hi >= sl_price: exit_px = sl_price; break
                if lo <= tp_price: exit_px = tp_price; break
                if trail_arm:
                    best = min(best, lo)
                    if best <= entry * (1 - trail_arm):
                        sl_price = min(sl_price, best * (1 + p["trail_dist"]))
        if exit_px is None:
            exit_px = float(df5["close"].iloc[j])
        gross = (exit_px - entry) / entry if long else (entry - exit_px) / entry
        trades.append(gross - 2 * TAKER_FEE)
        i = (j - WINDOW) + 1
    if not trades:
        return {"trades": 0, "winrate": 0, "total": 0, "expectancy": 0}
    wins = [t for t in trades if t > 0]
    return {"trades": len(trades), "winrate": len(wins) / len(trades),
            "total": sum(trades), "expectancy": sum(trades) / len(trades)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    args = ap.parse_args()

    config = Config.load(None)
    logger = setup_logger("backtest2", level="ERROR")
    db = DatabaseManager(config.postgresql, logger)
    sa = SentimentAgent(config, logger, db)
    agent = TradingDecisionAgent(config, logger, db, sa)

    data = {}
    for sym in args.symbols:
        df5 = load_5m(db, sym, args.days)
        if df5 is None or len(df5) < WINDOW + 10:
            print(f"{sym}: insufficient data"); continue
        inds = precompute(agent, df5)
        print(f"{sym}: {len(df5)} 5m bars ({df5['ts'].iloc[0].date()}..{df5['ts'].iloc[-1].date()})")
        data[sym] = (df5, inds)

    grid = {
        "sl_floor": [0.005, 0.008], "sl_atr": [1.0, 1.5],
        "rr": [2.0, 3.0, 4.0], "vol_gate": [0.0, 0.5],
        "trail": [0, 0.01], "trail_dist": [0.005],
    }
    keys = list(grid)
    combos = [dict(zip(keys, v)) for v in itertools.product(*grid.values())]

    results = []
    for sname, sfn in STRATS.items():
        for p in combos:
            agg = {"trades": 0, "wins": 0.0, "total": 0.0}
            for sym, (df5, inds) in data.items():
                r = simulate(df5, inds, sfn, p)
                agg["trades"] += r["trades"]; agg["wins"] += r["winrate"] * r["trades"]
                agg["total"] += r["total"]
            if agg["trades"] < 5:
                continue
            results.append({"strat": sname, **p, "trades": agg["trades"],
                            "winrate": agg["wins"] / agg["trades"],
                            "total_pct": agg["total"] * 100})

    results.sort(key=lambda r: r["total_pct"], reverse=True)
    print(f"\n{'='*104}\nTOP 15 by total return ({args.days}d, fees {2*TAKER_FEE*100:.2f}%/trade, MAX_HOLD {MAX_HOLD*5//60}h):\n")
    hdr = f"{'strat':>9}{'sl_fl':>6}{'sl_atr':>7}{'rr':>5}{'vgate':>6}{'trail':>6}{'trades':>7}{'win%':>7}{'tot%':>8}"
    print(hdr); print("-" * len(hdr))
    for r in results[:15]:
        print(f"{r['strat']:>9}{r['sl_floor']:>6.3f}{r['sl_atr']:>7.1f}{r['rr']:>5.1f}"
              f"{r['vol_gate']:>6.1f}{r['trail']:>6.2f}{r['trades']:>7d}{r['winrate']*100:>7.1f}{r['total_pct']:>8.2f}")
    # best per strategy
    print(f"\nBest per strategy:")
    for sname in STRATS:
        rs = [r for r in results if r["strat"] == sname]
        if rs:
            b = max(rs, key=lambda r: r["total_pct"])
            print(f"  {sname:>9}: {b['trades']} trades, {b['winrate']*100:.0f}% win, {b['total_pct']:+.2f}%  "
                  f"(sl{b['sl_floor']} atr{b['sl_atr']} rr{b['rr']} vg{b['vol_gate']} trail{b['trail']})")


if __name__ == "__main__":
    main()
