#!/usr/bin/env python
"""Forward-return tracker for LIVE signals — the only honest way to measure the LLM's
informational edge (not backtestable). For each BUY/SELL signal in the DB, compute the
directional forward return at +1h and +4h using ohlcv_raw, then summarise hit-rate and
average edge BY SOURCE (gpt-5.4 / glm / rule-based / vol_gate). Run periodically; the
sample grows as the gated system emits signals.

Usage: python tools/signal_edge.py [--hours 720]
"""
import os, sys, argparse
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_env(p):
    d = {}
    try:
        for l in open(p):
            s = l.strip()
            if s and '=' in s and not s.startswith('#'):
                k, v = s.split('=', 1); d[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=720)
    args = ap.parse_args()
    env = load_env('/home/andy/.env'); env.update(load_env('/home/andy/CryptoTrader/.env'))
    import psycopg2
    c = psycopg2.connect(host=env.get('POSTGRES_HOST'), dbname=env.get('POSTGRES_DB'),
                         user=env.get('POSTGRES_USER'), password=env.get('POSTGRES_PASSWORD'), connect_timeout=8)
    cur = c.cursor()
    cur.execute("""
        SELECT s.symbol, s.signal_type, s.price, s.timestamp, s.confidence, s.model_version
        FROM signals s
        WHERE s.signal_type IN ('BUY','SELL') AND s.price IS NOT NULL
          AND s.timestamp >= now() - interval '%s hours'
        ORDER BY s.timestamp
    """ % args.hours)
    sigs = cur.fetchall()
    print(f"BUY/SELL signals in window: {len(sigs)}")
    if not sigs:
        print("No directional signals yet — tracker accrues as the gated system runs."); c.close(); return

    def fwd_price(symbol, ts, minutes):
        cur.execute("""SELECT close FROM ohlcv_raw WHERE symbol=%s AND timeframe='1m'
                       AND timestamp >= %s + interval '%s minutes' ORDER BY timestamp LIMIT 1""",
                    (symbol, ts, minutes))
        r = cur.fetchone()
        return float(r[0]) if r else None

    by_src = defaultdict(lambda: {"n": 0, "r1": [], "r4": []})
    for symbol, side, price, ts, conf, src in sigs:
        price = float(price); src = src or "unknown"
        for horizon, key in ((60, "r1"), (240, "r4")):
            fp = fwd_price(symbol, ts, horizon)
            if fp is None:
                continue
            ret = (fp - price) / price if side == 'BUY' else (price - fp) / price
            by_src[src][key].append(ret)
        by_src[src]["n"] += 1

    print(f"\n{'source':22}{'n':>4}{'+1h hit%':>10}{'+1h avg%':>10}{'+4h hit%':>10}{'+4h avg%':>10}")
    print("-" * 76)
    for src, d in sorted(by_src.items(), key=lambda x: -x[1]["n"]):
        def stats(rs):
            if not rs: return (float('nan'), float('nan'))
            hit = sum(1 for r in rs if r > 0) / len(rs) * 100
            return hit, sum(rs) / len(rs) * 100
        h1, a1 = stats(d["r1"]); h4, a4 = stats(d["r4"])
        print(f"{src:22}{d['n']:>4}{h1:>10.0f}{a1:>+10.3f}{h4:>10.0f}{a4:>+10.3f}")
    print("\nNote: 'avg%' is gross directional move (no fees). A real edge needs avg% to clear "
          "round-trip fees (~0.04% maker / ~0.11% taker). Sample must be large to trust.")
    c.close()


if __name__ == "__main__":
    main()
