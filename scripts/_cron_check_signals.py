#!/usr/bin/env python3
"""Check pending linear signals in cryptotrader DB."""
import sys, os, psycopg2

base = '/home/andy'
trader = base + '/CryptoTrader_old'

sys.path.insert(0, trader)
from dotenv import load_dotenv
dotenv_path = trader + '/.env'
load_dotenv(dotenv_path)

pg_pw = os.getenv('POSTGRES_PASSWORD')
conn = psycopg2.connect(host='127.0.0.1', port=5433, user='cryptotrader', password=pg_pw, dbname='cryptotrader')
cur = conn.cursor()

# Check lowercase 'pending' (as stored in DB)
cur.execute("SELECT id, symbol, signal_type, confidence, status, market_type, created_at FROM signals WHERE market_type='linear' AND status='pending' ORDER BY created_at DESC LIMIT 10")
rows = cur.fetchall()
print(f"pending (lowercase): {len(rows)}")
for r in rows:
    print(f"  id={r[0]} {r[1]} {r[2]} conf={r[3]} status={r[4]} market={r[5]} at={r[6]}")

# Check uppercase 'PENDING'
cur.execute("SELECT id, symbol, signal_type, confidence, status, market_type, created_at FROM signals WHERE market_type='linear' AND status='PENDING' ORDER BY created_at DESC LIMIT 10")
rows = cur.fetchall()
print(f"\nPENDING (uppercase): {len(rows)}")
for r in rows:
    print(f"  id={r[0]} {r[1]} {r[2]} conf={r[3]} status={r[4]} market={r[5]} at={r[6]}")

# Also check recent signals regardless of status
cur.execute("SELECT id, symbol, signal_type, confidence, status, market_type, created_at FROM signals WHERE market_type='linear' ORDER BY created_at DESC LIMIT 10")
rows = cur.fetchall()
print(f"\nRecent linear signals: {len(rows)}")
for r in rows:
    print(f"  id={r[0]} {r[1]} {r[2]} conf={r[3]} status={r[4]} market={r[5]} at={r[6]}")

# Check spot signals too for comparison
cur.execute("SELECT id, symbol, signal_type, confidence, status, market_type, created_at FROM signals WHERE market_type='spot' AND status='pending' ORDER BY created_at DESC LIMIT 5")
rows = cur.fetchall()
print(f"\nPending spot signals: {len(rows)}")
for r in rows:
    print(f"  id={r[0]} {r[1]} {r[2]} conf={r[3]} status={r[4]} market={r[5]} at={r[6]}")

cur.close()
conn.close()
