#!/usr/bin/env python3
"""Debug v6 - показать сработавшие бонусы VSA/Spring/UTAD."""
import os, sys
from datetime import datetime, timezone
sys.path.insert(0, '/home/andy/CryptoTrader')
from cryptotrader_strategies.clone_5_v6 import Clone5V6Strategy
import pandas as pd
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')
import psycopg2

DB = dict(host="192.168.0.149", port=5432, database="cryptotrader", user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"])

# Загружаем данные для каждой пары и считаем срабатывания
SYMS = ["TONUSDT", "DOGEUSDT", "SUIUSDT"]
START = datetime(2026, 3, 7, tzinfo=timezone.utc)
END = datetime(2026, 6, 6, 18, tzinfo=timezone.utc)

conn = psycopg2.connect(**DB)
for sym in SYMS:
    cur = conn.cursor()
    cur.execute("""
        SELECT timestamp, open, high, low, close, volume
        FROM ohlcv_raw WHERE exchange='bybit' AND timeframe='5m' AND symbol=%s
        AND timestamp >= %s AND timestamp <= %s
        ORDER BY timestamp
    """, (sym, START, END))
    rows = cur.fetchall()
    if len(rows) < 100:
        continue
    df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df.set_index('timestamp', inplace=True)
    df = df.astype(float)

    c = Clone5V6Strategy()
    c.params.symbols = [sym]
    bull_count = bear_count = 0
    bonuses = {'vsa_absorb': 0, 'vsa_climax': 0, 'vsa_no_demand': 0, 'vsa_no_supply': 0, 'spring': 0, 'utad': 0, 'eql': 0, 'near_round': 0}
    for last in range(c.swing_lookback + 5, len(df) - 1):
        b = c._detect_bullish_hunt(df, last, sym)
        if b:
            bull_count += 1
            if b['is_absorbing']: bonuses['vsa_absorb'] += 1
            if b['is_climax']: bonuses['vsa_climax'] += 1
            if b.get('is_spring', False): bonuses['spring'] += 1
            if b['eql_count'] >= c.min_equal_lows_count: bonuses['eql'] += 1
            if b['near_round']: bonuses['near_round'] += 1
        br = c._detect_bearish_hunt(df, last, sym)
        if br:
            bear_count += 1
            if br['is_absorbing']: bonuses['vsa_absorb'] += 1
            if br['is_climax']: bonuses['vsa_climax'] += 1
            if br.get('is_utad', False): bonuses['utad'] += 1
            if br['eqh_count'] >= c.min_equal_lows_count: bonuses['eql'] += 1
            if br['near_round']: bonuses['near_round'] += 1
    print(f"\n{sym}:")
    print(f"  bull: {bull_count}, bear: {bear_count}")
    for k, v in bonuses.items():
        if v > 0:
            print(f"  {k}: {v}")
    cur.close()
conn.close()
