#!/usr/bin/env python
"""Rule-based backtest over historical 5m bars (resampled from ohlcv_raw 1m).

Backtests the DETERMINISTIC rule-based engine (TradingDecisionAgent._rule_based_decision_
from_indicators) using the project's own calculate_indicators() on a rolling 200-bar window,
so signals match live behaviour exactly. NB: live decisions are made by gpt-5.4 (primary);
rule-based is the tunable fallback — this tunes SL/TP floors, ATR multipliers, min_confidence
and a volume gate, which apply regardless of decision source.

Indicators + rule-based signal do NOT depend on the tuned params, so we compute them once per
bar, then replay the cheap SL/TP simulation across a parameter grid.

Usage: python tools/backtest.py [--days 30] [--symbols BTCUSDT ETHUSDT] [--tf 5m]
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

TAKER_FEE = 0.00055  # Bybit linear taker, per side
WINDOW = 200          # bars fed to calculate_indicators (matches live)
MAX_HOLD = 24         # 5m bars (~2h) — scalping hold cap


def load_5m(db, symbol, days):
    """Load 1m from ohlcv_raw and resample to 5m."""
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
    """One pass: per bar i (>=WINDOW) compute indicators + rule-based decision."""
    out = []
    for i in range(WINDOW, len(df5)):
        window = df5.iloc[i - WINDOW:i + 1][["open", "high", "low", "close", "volume"]]
        ind = agent.calculate_indicators(window)
        if not ind:
            out.append(None); continue
        dec = agent._rule_based_decision_from_indicators(ind)
        atr_pct = (ind.get("atr_14", 0) / ind["price"]) if ind.get("price") else 0
        out.append({"signal": dec["signal"], "conf": dec["confidence"],
                    "atr_pct": atr_pct, "vol_ratio": ind.get("volume_ratio", 1.0)})
    return out  # index offset by WINDOW


def simulate(df5, decs, p):
    """Replay SL/TP for one param set. Returns stats dict."""
    n = len(df5)
    trades = []
    i = 0
    while i < len(decs) - 1:
        d = decs[i]
        bar = i + WINDOW
        if (d is None or d["signal"] not in ("BUY", "SELL")
                or d["conf"] < p["min_conf"] or d["vol_ratio"] < p["vol_gate"]):
            i += 1; continue
        entry = float(df5["open"].iloc[bar + 1]) if bar + 1 < n else None
        if entry is None:
            break
        sl_pct = max(p["sl_floor"], p["sl_atr"] * d["atr_pct"])
        tp_pct = max(p["tp_floor"], p["tp_atr"] * d["atr_pct"])
        long = d["signal"] == "BUY"
        sl_price = entry * (1 - sl_pct) if long else entry * (1 + sl_pct)
        tp_price = entry * (1 + tp_pct) if long else entry * (1 - tp_pct)
        exit_px, hit = None, None
        for j in range(bar + 1, min(bar + 1 + MAX_HOLD, n)):
            hi, lo = float(df5["high"].iloc[j]), float(df5["low"].iloc[j])
            if long:
                if lo <= sl_price: exit_px, hit = sl_price, "SL"; break
                if hi >= tp_price: exit_px, hit = tp_price, "TP"; break
            else:
                if hi >= sl_price: exit_px, hit = sl_price, "SL"; break
                if lo <= tp_price: exit_px, hit = tp_price, "TP"; break
            last_j = j
        if exit_px is None:
            exit_px, hit = float(df5["close"].iloc[last_j]), "TIME"
            j = last_j
        gross = (exit_px - entry) / entry if long else (entry - exit_px) / entry
        net = gross - 2 * TAKER_FEE
        trades.append(net)
        i = (j - WINDOW) + 1  # resume after exit (1 position at a time)
    if not trades:
        return {"trades": 0, "winrate": 0, "expectancy": 0, "total": 0}
    wins = [t for t in trades if t > 0]
    return {"trades": len(trades), "winrate": len(wins) / len(trades),
            "expectancy": sum(trades) / len(trades), "total": sum(trades)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    args = ap.parse_args()

    config = Config.load(None)
    logger = setup_logger("backtest", level="WARNING")
    db = DatabaseManager(config.postgresql, logger)
    sa = SentimentAgent(config, logger, db)
    agent = TradingDecisionAgent(config, logger, db, sa)

    grid = {
        "sl_floor": [0.003, 0.005], "tp_floor": [0.006, 0.010],
        "sl_atr": [1.0, 1.5], "tp_atr": [1.7, 2.5],
        "min_conf": [0.65, 0.75], "vol_gate": [0.0, 0.4],
    }
    keys = list(grid)
    combos = [dict(zip(keys, v)) for v in itertools.product(*grid.values())]

    # precompute per symbol
    data = {}
    for sym in args.symbols:
        df5 = load_5m(db, sym, args.days)
        if df5 is None or len(df5) < WINDOW + 10:
            print(f"{sym}: insufficient data"); continue
        decs = precompute(agent, df5)
        n_sig = sum(1 for d in decs if d and d["signal"] in ("BUY", "SELL"))
        print(f"{sym}: {len(df5)} 5m bars ({df5['ts'].iloc[0].date()}..{df5['ts'].iloc[-1].date()}), "
              f"{n_sig} raw rule-based signals")
        data[sym] = (df5, decs)

    # evaluate grid (aggregate across symbols)
    results = []
    for p in combos:
        agg = {"trades": 0, "wins": 0.0, "total": 0.0}
        for sym, (df5, decs) in data.items():
            r = simulate(df5, decs, p)
            agg["trades"] += r["trades"]
            agg["wins"] += r["winrate"] * r["trades"]
            agg["total"] += r["total"]
        if agg["trades"] == 0:
            continue
        results.append({
            **p, "trades": agg["trades"],
            "winrate": agg["wins"] / agg["trades"],
            "expectancy": agg["total"] / agg["trades"],
            "total_pct": agg["total"] * 100,
        })

    results.sort(key=lambda r: r["total_pct"], reverse=True)
    print(f"\n{'='*100}\nTOP 12 param sets by total return ({args.days}d, fees {2*TAKER_FEE*100:.2f}%/trade):\n")
    hdr = f"{'sl_fl':>6}{'tp_fl':>6}{'sl_atr':>7}{'tp_atr':>7}{'minc':>6}{'vgate':>6}{'trades':>7}{'win%':>7}{'exp%':>8}{'tot%':>8}"
    print(hdr); print("-" * len(hdr))
    for r in results[:12]:
        print(f"{r['sl_floor']:>6.3f}{r['tp_floor']:>6.3f}{r['sl_atr']:>7.1f}{r['tp_atr']:>7.1f}"
              f"{r['min_conf']:>6.2f}{r['vol_gate']:>6.1f}{r['trades']:>7d}{r['winrate']*100:>7.1f}"
              f"{r['expectancy']*100:>8.3f}{r['total_pct']:>8.2f}")
    print(f"\n{'='*100}\nWORST 3:")
    for r in results[-3:]:
        print(f"  sl{r['sl_floor']} tp{r['tp_floor']} minc{r['min_conf']} vg{r['vol_gate']} "
              f"-> {r['trades']} trades, {r['winrate']*100:.0f}% win, {r['total_pct']:.2f}% total")


if __name__ == "__main__":
    main()
