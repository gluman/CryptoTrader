#!/usr/bin/env python3
"""
Sync executed trade to PostgreSQL database.
Called after a successful order execution.

Usage:
  python3 sync_trade_to_db.py --symbol STGUSDT --side Sell --qty 700 --entry 0.2719 --sl 0.286 --tp 0.244 --leverage 5 --order-id abc123
"""
import sys, os, argparse
from datetime import datetime, timezone

# Load .env
ENV_PATH = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(ENV_PATH):
    with open(ENV_PATH) as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                os.environ[k] = v

import psycopg2

def get_conn():
    return psycopg2.connect(
        host=os.environ.get('POSTGRES_HOST', '192.168.0.149'),
        port=int(os.environ.get('POSTGRES_PORT', 5432)),
        dbname=os.environ.get('POSTGRES_DB', 'cryptotrader'),
        user=os.environ.get('POSTGRES_USER', 'cryptotrader'),
        password=os.environ.get('POSTGRES_PASSWORD', '')
    )

def sync_open(args):
    """Record new position opening."""
    conn = get_conn()
    cur = conn.cursor()
    
    entry = float(args.entry)
    qty = float(args.qty)
    leverage = int(args.leverage)
    notional = entry * qty
    cost = notional / leverage
    
    side_db = 'long' if args.side.lower() == 'buy' else 'short'
    
    cur.execute("""
        INSERT INTO positions 
        (symbol, exchange, side, entry_price, quantity, cost_usdt, stop_loss, take_profit, 
         leverage, status, opened_at, market_type, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        args.symbol, 'bybit', side_db, entry, qty, cost,
        float(args.sl), float(args.tp), leverage,
        'open', datetime.now(timezone.utc), 'linear',
        datetime.now(timezone.utc), datetime.now(timezone.utc)
    ))
    
    row = cur.fetchone()
    if not row:
        conn.rollback()
        cur.close()
        conn.close()
        print("ERROR: INSERT failed, no ID returned")
        sys.exit(1)
    pos_id = row[0]
    conn.commit()
    
    print(f"DB: Position {pos_id} opened: {args.symbol} {side_db} qty={qty} entry={entry}")
    
    cur.close()
    conn.close()
    return pos_id

def sync_close(args):
    """Record position closing."""
    conn = get_conn()
    cur = conn.cursor()
    
    close_price = float(args.close_price)
    
    cur.execute("""
        UPDATE positions SET 
            status = 'closed',
            close_price = %s,
            closed_at = %s,
            updated_at = %s
        WHERE symbol = %s AND UPPER(status) = 'OPEN'
        RETURNING id, entry_price, quantity, side
    """, (close_price, datetime.now(timezone.utc), datetime.now(timezone.utc), args.symbol))
    
    result = cur.fetchone()
    if result:
        pos_id, entry_price, quantity, side = result
        if side == 'long':
            pnl = (close_price - float(entry_price)) * float(quantity)
        else:
            pnl = (float(entry_price) - close_price) * float(quantity)
        
        cur.execute("""
            UPDATE positions SET realized_pnl = %s, updated_at = %s WHERE id = %s
        """, (pnl, datetime.now(timezone.utc), pos_id))
        
        conn.commit()
        print(f"DB: Position {pos_id} closed: {args.symbol} PnL={pnl:.4f}")
    else:
        print(f"DB: No open position found for {args.symbol}")
    
    conn.commit()
    cur.close()
    conn.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--action', required=True, choices=['open', 'close'])
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--side', default='Buy')
    parser.add_argument('--qty', default='0')
    parser.add_argument('--entry', default='0')
    parser.add_argument('--sl', default='0')
    parser.add_argument('--tp', default='0')
    parser.add_argument('--leverage', default='5')
    parser.add_argument('--close-price', default='0')
    parser.add_argument('--order-id', default='')
    args = parser.parse_args()
    
    if args.action == 'open':
        sync_open(args)
    elif args.action == 'close':
        sync_close(args)
