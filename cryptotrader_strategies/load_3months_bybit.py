#!/usr/bin/env python3
"""
Догрузка 5m OHLCV с Bybit API за последние 90 дней для пар Clone5.
Top pairs by data completeness in DB.
"""
import os
import sys
import time
import json
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import psycopg2
import pandas as pd
from dotenv import load_dotenv

load_dotenv('/home/andy/CryptoTrader/.env')

# Пары Clone5 (по настройкам)
SYMBOLS = [
    "XRPUSDT", "DOGEUSDT", "TONUSDT", "SUIUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "HYPEUSDT",
]

# Скачиваем с 1 марта 2026 = ~95 дней назад
END_TS = int(datetime(2026, 6, 6, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
START_TS = int(datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
TF = "5m"
EXCHANGE = "bybit"

DB = dict(
    host="192.168.0.149", port=5432, database="cryptotrader",
    user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"],
)

exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})


def fetch_chunk(symbol, since_ms, limit=1000):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, TF, since=since_ms, limit=limit)
        return ohlcv
    except Exception as e:
        print(f"  fetch error {symbol}: {e}", flush=True)
        return []


def insert_chunk(rows, symbol):
    if not rows:
        return 0
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    inserted = 0
    for ts_ms, o, h, l, c, v in rows:
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        try:
            cur.execute("""
                INSERT INTO ohlcv_raw (exchange, timeframe, symbol, timestamp, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (exchange, timeframe, symbol, timestamp) DO NOTHING
            """, (EXCHANGE, TF, symbol, ts, o, h, l, c, v))
            if cur.rowcount > 0:
                inserted += 1
        except Exception as e:
            print(f"  insert err {symbol} {ts}: {e}", flush=True)
    conn.commit()
    cur.close()
    conn.close()
    return inserted


def load_symbol(symbol):
    print(f"=== {symbol} ===", flush=True)
    since = START_TS
    end = END_TS
    total = 0
    while since < end:
        rows = fetch_chunk(symbol, since, limit=1000)
        if not rows:
            print(f"  empty chunk at since={since}", flush=True)
            break
        last_ts = rows[-1][0]
        ins = insert_chunk(rows, symbol)
        total += ins
        since = last_ts + 1
        if last_ts >= end - 60000:
            break
        # Bybit rate-limit: 50 req/5s on linear. ~1 req/sec OK.
        time.sleep(0.25)
    print(f"  {symbol} done: inserted={total}", flush=True)
    return total


def main():
    summary = {}
    t0 = time.time()
    for s in SYMBOLS:
        try:
            n = load_symbol(s)
            summary[s] = n
        except Exception as e:
            print(f"!! {s} failed: {e}", flush=True)
            summary[s] = -1
    dt = time.time() - t0
    print(f"\nDONE in {dt:.1f}s: {json.dumps(summary, indent=2)}", flush=True)


if __name__ == "__main__":
    main()
