#!/usr/bin/env python3
"""
Pre-execution validation for CryptoTrader LLM trades.
Checks money management rules BEFORE placing any order.

Exit codes:
  0 = PASS — all checks passed
  1 = REJECT — trade rejected (reason in stdout)

Usage:
  python3 pre_execution_check.py --symbol STGUSDT --side Sell --entry 0.2719 --sl 0.286 --tp 0.244 --qty 700
"""
import sys, os, json, argparse

# Load .env
ENV_PATH = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(ENV_PATH):
    with open(ENV_PATH) as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                os.environ[k] = v

import ccxt

# === CONFIG ===
MAX_POSITION_PCT = 0.20      # Max 20% of balance per position
MAX_SL_PCT = 0.02            # Max 2% SL from entry
MIN_RR_RATIO = 1.5           # Min risk:reward ratio
MAX_CONCURRENT_POSITIONS = 3 # Max open positions
DAILY_LOSS_LIMIT_PCT = 0.10  # Stop trading if daily loss > 10% of balance
MIN_BALANCE_USDT = 5.0       # Minimum balance to trade
MAX_LEVERAGE = 10             # Maximum allowed leverage


def get_exchange():
    ex = ccxt.bybit({
        'options': {'defaultType': 'linear'},
        'enableRateLimit': True
    })
    ex.apiKey = os.environ.get('BYBIT_API_KEY', '')
    ex.secret = os.environ.get('BYBIT_API_SECRET', '')
    return ex


def check_balance(ex):
    """Check if enough free balance for trade."""
    bal = ex.fetch_balance({'type': 'swap', 'coin': 'USDT'})
    total = float(bal.get('USDT', {}).get('total', 0) or 0)
    free = float(bal.get('USDT', {}).get('free', 0) or 0)
    return total, free


def get_open_positions(ex):
    """Get list of open positions."""
    positions = ex.fetch_positions()
    return [p for p in positions if abs(float(p.get('contracts', 0) or 0)) > 0]


def run_checks(args):
    ex = get_exchange()
    errors = []
    warnings = []
    
    # 1. Check balance
    try:
        total_balance, free_balance = check_balance(ex)
    except Exception as e:
        print(f"REJECT: Cannot fetch balance: {e}")
        return 1
    
    if total_balance < MIN_BALANCE_USDT:
        print(f"REJECT: Balance too low: ${total_balance:.2f} < ${MIN_BALANCE_USDT}")
        return 1
    
    # 2. Check existing positions count
    try:
        positions = get_open_positions(ex)
    except Exception as e:
        print(f"REJECT: Cannot fetch positions: {e}")
        return 1
    
    # Check if symbol already has a position
    for p in positions:
        if p['symbol'] == args.symbol:
            print(f"REJECT: Position already open for {args.symbol}")
            return 1
    
    if len(positions) >= MAX_CONCURRENT_POSITIONS:
        print(f"REJECT: Max concurrent positions reached: {len(positions)}/{MAX_CONCURRENT_POSITIONS}")
        return 1
    
    # 3. Calculate position cost and check size
    entry = float(args.entry)
    qty = float(args.qty)
    notional = entry * qty
    leverage = int(args.leverage) if args.leverage else 5
    margin = notional / leverage
    
    max_margin = total_balance * MAX_POSITION_PCT
    if margin > max_margin:
        errors.append(f"Position margin ${margin:.2f} > {MAX_POSITION_PCT*100}% of balance ${max_margin:.2f}")
    
    # 4. Check SL distance
    sl = float(args.sl)
    if args.side.lower() == 'buy':
        sl_pct = (entry - sl) / entry
    else:
        sl_pct = (sl - entry) / entry
    
    if sl_pct > MAX_SL_PCT:
        errors.append(f"SL distance {sl_pct*100:.2f}% > max {MAX_SL_PCT*100}%")
    
    # 5. Check R:R ratio
    tp = float(args.tp)
    if args.side.lower() == 'buy':
        reward = tp - entry
    else:
        reward = entry - tp
    
    risk = abs(entry - sl)
    rr = 0.0
    if risk > 0:
        rr = reward / risk
        if rr < MIN_RR_RATIO - 0.001:  # small epsilon for float comparison
            errors.append(f"R:R {rr:.2f} < min {MIN_RR_RATIO}")
    
    # 6. Check leverage
    if leverage > MAX_LEVERAGE:
        errors.append(f"Leverage {leverage}x > max {MAX_LEVERAGE}x")
    
    # 7. Daily loss limit check (from DB)
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ.get('POSTGRES_HOST', '192.168.0.149'),
            port=int(os.environ.get('POSTGRES_PORT', 5432)),
            dbname=os.environ.get('POSTGRES_DB', 'cryptotrader'),
            user=os.environ.get('POSTGRES_USER', 'cryptotrader'),
            password=os.environ.get('POSTGRES_PASSWORD', '')
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT SUM(unrealized_pnl) FROM positions 
            WHERE UPPER(status) = 'OPEN'
        """)
        result = cur.fetchone()
        open_pnl = float(result[0] or 0) if result and result[0] else 0
        
        # Get equity
        equity = total_balance + open_pnl
        if equity < total_balance * (1 - DAILY_LOSS_LIMIT_PCT):
            errors.append(f"Daily loss limit: equity ${equity:.2f} < {DAILY_LOSS_LIMIT_PCT*100}% of balance ${total_balance:.2f}")
        
        conn.close()
    except Exception as e:
        warnings.append(f"DB check skipped: {e}")
    
    # 8. Free balance check
    if free_balance < margin:
        errors.append(f"Free balance ${free_balance:.2f} < required margin ${margin:.2f}")
    
    # Result
    if errors:
        for err in errors:
            print(f"REJECT: {err}")
        return 1
    
    # Print validation summary
    print(f"PASS: All checks passed")
    print(f"  Symbol: {args.symbol}")
    print(f"  Side: {args.side}")
    print(f"  Entry: {entry}")
    print(f"  SL: {sl} ({sl_pct*100:.2f}%)")
    print(f"  TP: {tp}")
    print(f"  R:R: {rr:.2f}")
    print(f"  Qty: {qty}")
    print(f"  Notional: ${notional:.2f}")
    print(f"  Margin: ${margin:.2f} ({margin/total_balance*100:.1f}% of balance)")
    print(f"  Leverage: {leverage}x")
    print(f"  Balance: ${total_balance:.2f} (free: ${free_balance:.2f})")
    print(f"  Open positions: {len(positions)}/{MAX_CONCURRENT_POSITIONS}")
    if warnings:
        for w in warnings:
            print(f"  WARNING: {w}")
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--side', required=True, choices=['Buy', 'Sell', 'buy', 'sell'])
    parser.add_argument('--entry', required=True, type=str)
    parser.add_argument('--sl', required=True, type=str)
    parser.add_argument('--tp', required=True, type=str)
    parser.add_argument('--qty', required=True, type=str)
    parser.add_argument('--leverage', default='5', type=str)
    args = parser.parse_args()
    
    sys.exit(run_checks(args))
