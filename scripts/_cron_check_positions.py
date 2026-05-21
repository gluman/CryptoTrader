#!/usr/bin/env python3
"""Check today's trades, open positions, and stale positions."""
import sys, os, psycopg2

base = '/home/andy'
trader = base + '/CryptoTrader_old'
sys.path.insert(0, trader)
from dotenv import load_dotenv
load_dotenv(trader + '/.env')
pg_pw = os.getenv('POSTGRES_PASSWORD')
conn = psycopg2.connect(host='127.0.0.1', port=5433, user='cryptotrader', password=pg_pw, dbname='cryptotrader')
cur = conn.cursor()

# Today's closed trades
cur.execute("""
    SELECT id, symbol, side, quantity, entry_price, close_price,
           realized_pnl, realized_pnl_percent,
           EXTRACT(EPOCH FROM (closed_at - opened_at)) as dur, closed_at
    FROM positions
    WHERE status='closed' AND market_type='linear'
      AND DATE(closed_at) = CURRENT_DATE
    ORDER BY closed_at DESC LIMIT 10
""")
rows = cur.fetchall()
print(f'Closed positions today: {len(rows)}')
for r in rows:
    pnl = float(r[6]) if r[6] else 0
    pnl_pct = float(r[7]) if r[7] else 0
    dur = float(r[8]) if r[8] else 0
    print(f'  id={r[0]} {r[1]} {r[2]} qty={r[3]} entry={r[4]} close={r[5]} pnl={pnl:.2f} ({pnl_pct:.2f}%) dur={dur:.0f}s at={r[9]}')

# Open positions
cur.execute("""
    SELECT id, symbol, side, quantity, entry_price,
           stop_loss, take_profit, unrealized_pnl, unrealized_pnl_percent,
           status, opened_at
    FROM positions WHERE status='open' AND market_type='linear'
    ORDER BY opened_at DESC
""")
rows = cur.fetchall()
print(f'\nOpen positions: {len(rows)}')
for r in rows:
    upnl = float(r[7]) if r[7] else 0
    upnl_pct = float(r[8]) if r[8] else 0
    print(f'  id={r[0]} {r[1]} {r[2]} qty={r[3]} entry={r[4]} sl={r[5]} tp={r[6]} upnl={upnl:.2f} ({upnl_pct:.2f}%) {r[9]} at={r[10]}')

# Stale positions
cur.execute("""
    SELECT id, symbol, side, quantity, entry_price, status
    FROM positions WHERE market_type='linear'
      AND (quantity=0 OR entry_price=0) AND status='open'
    ORDER BY id
""")
rows = cur.fetchall()
print(f'\nStale/broken positions (qty=0 or entry=0): {len(rows)}')
for r in rows:
    print(f'  id={r[0]} {r[1]} {r[2]} qty={r[3]} entry={r[4]} status={r[5]}')

cur.close()
conn.close()
