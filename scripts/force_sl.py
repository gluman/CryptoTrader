#!/usr/bin/env python3
"""Test SL/TP with guaranteed trigger: SL at entry price"""
import sys; sys.path.insert(0, '.')
import time
from src.core.config import Config; Config._instance = None; config = Config.load()
from src.core.logger import setup_logger
from src.core.database import DatabaseManager, Trade, Position
from src.agents.execution_agent import ExecutionAgent
from decimal import Decimal

logger = setup_logger('sl_test', level='INFO')
db = DatabaseManager(config.postgresql, logger)
executor = ExecutionAgent(config, db, logger)
target = 'ENJUSDT'
amount = 5.0

# 1. BUY
print("=== GUARANTEED SL TEST ===")
qty, order = executor.execute_spot_buy(target, amount, 'bybit')
print(f"BUY @ ${order.get('price', order.get('avg_price','?'))} qty={qty}")
print(f"Order: {order['order_id']}")

# wait a tick
time.sleep(2)

# 2. Find the position and set SL = entry price
with db.get_session() as s:
    pos = s.query(Position).filter_by(
        symbol=target, status='OPEN', side='LONG'
    ).order_by(Position.id.desc()).first()
    if not pos:
        print("ERROR: no position found")
        sys.exit(1)
    entry = float(pos.entry_price)
    pos.stop_loss = pos.entry_price  # SL exactly at entry = instant trigger
    pos.take_profit = pos.entry_price * Decimal('1.5')  # TP far away
    s.commit()
    print(f"Position #{pos.id}: entry=${entry:.4f}, SL=${float(pos.stop_loss):.4f} (GUARANTEED)")

# 3. Check SL/TP
print("\nChecking SL/TP...")
triggers = executor.check_stop_loss_take_profit()
print(f"Triggers found: {len(triggers)}")

if triggers:
    for t in triggers:
        print(f"  {t['type'].upper()}: pos #{t['position_id']} {t['symbol']} "
              f"price=${float(t['current_price']):.4f} vs "
              f"{'SL' if t['type']=='stop_loss' else 'TP'}=${float(t['sl_or_tp']):.4f}")
    
    # 4. Process triggers
    result = executor._process_sl_tp_triggers(triggers)
    print(f"\nSL processed: {result}")
else:
    print("NO triggers - current price didn't match SL")

# 5. Verify
time.sleep(2)
with db.get_session() as s:
    pos = s.query(Position).filter_by(id=pos.id).first()
    print(f"\nPosition #{pos.id} status: {pos.status}")
    print(f"PnL: ${float(pos.pnl or 0):+.4f} ({float(pos.pnl_percent or 0):+.2f}%)")
    
    last_trade = s.query(Trade).filter_by(position_id=pos.id).order_by(Trade.id.desc()).first()
    if last_trade:
        print(f"Close trade #{last_trade.id}: {last_trade.side} @ ${float(last_trade.price):.4f}")
        print(f"Trade PnL: ${float(last_trade.pnl or 0):+.4f} ({float(last_trade.pnl_percent or 0):+.2f}%)")

print(f"\nSL: {'✅ TRIGGERED' if pos.status == 'CLOSED' else '❌ NOT TRIGGERED'}")
print(f"Total cost: {float(pos.pnl or 0):+.4f} USDT")
