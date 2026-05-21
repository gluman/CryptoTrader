#!/usr/bin/env python3
"""SL/TP tests with guaranteed triggers — SL/TP at entry price"""
import sys; sys.path.insert(0, '.')
import time, json
from src.core.config import Config; Config._instance = None; config = Config.load()
from src.core.logger import setup_logger
from src.core.database import DatabaseManager, Position, Trade
from src.agents.execution_agent import ExecutionAgent
from decimal import Decimal

logger = setup_logger('sl_tp_test', level='INFO')
db = DatabaseManager(config.postgresql, logger)
executor = ExecutionAgent(config, logger, db)

def get_price(symbol='ENJUSDT'):
    """Get current price from Bybit"""
    from src.gateways.bybit_api import BybitAPI
    ex = BybitAPI(config.bybit['api_key'], config.bybit['api_secret'])
    ticker = ex.get_ticker(symbol)
    return float(ticker['lastPrice'])

def run_test(test_type):
    """test_type: 'sl' or 'tp'"""
    print(f"\n{'='*60}")
    print(f"  GUARANTEED {test_type.upper()} TEST")
    print(f"{'='*60}")
    
    target = 'ENJUSDT'
    price = get_price(target)
    print(f"  Market: ${price:.4f}")
    
    # 1. BUY
    buy_result = executor.execute_spot_buy(target, str(round(5.0, 2)), 'bybit')
    if buy_result.get('error'):
        print(f"  ❌ BUY failed: {buy_result['error']}")
        return None
    print(f"  BUY executed: order={buy_result.get('order_id')}")
    time.sleep(2)
    
    # 2. Get fresh price and position
    price = get_price(target)
    
    with db.get_session() as s:
        pos = s.query(Position).filter_by(
            symbol=target, status='OPEN', side='LONG'
        ).order_by(Position.id.desc()).first()
        if not pos:
            print("  ❌ No open position found")
            return None
        
        entry = float(pos.entry_price)
        print(f"  Position #{pos.id}: entry=${entry:.4f}")
        
        if test_type == 'sl':
            pos.stop_loss = pos.entry_price       # SL at entry = guaranteed trigger
            pos.take_profit = pos.entry_price * Decimal('1.5')  # far away
            print(f"  SL set = ${entry:.4f} (entry level — guaranteed trigger)")
        else:
            pos.stop_loss = pos.entry_price * Decimal('0.5')   # far below
            pos.take_profit = pos.entry_price     # TP at entry = guaranteed trigger
            print(f"  TP set = ${entry:.4f} (entry level — guaranteed trigger)")
        
        s.commit()
    
    # 3. Check SL/TP with current price
    print(f"\n  Checking {test_type.upper()} with current price ${price:.4f}...")
    triggers = executor.check_stop_loss_take_profit(target, price)
    print(f"  Triggers found: {len(triggers)}")
    
    if triggers:
        for t in triggers:
            print(f"    {t['type']}: pos #{t['position_id']} → "
                  f"trigger=${float(t['trigger_price']):.4f} at ${float(t['current_price']):.4f}")
        
        # 4. Process
        result = executor._process_sl_tp_triggers(triggers)
        time.sleep(2)
        
        # 5. Verify
        with db.get_session() as s:
            pos = s.query(Position).filter_by(id=pos.id).first()
            print(f"\n  Position #{pos.id}: status={pos.status}")
            print(f"  PnL: ${float(pos.pnl or 0):+.4f} ({float(pos.pnl_percent or 0):+.2f}%)")
            
            trades = s.query(Trade).filter_by(position_id=pos.id).order_by(Trade.id.desc()).all()
            for t in trades:
                print(f"  Trade #{t.id}: {t.side} @ ${float(t.price):.4f} | "
                      f"PnL=${float(t.pnl or 0):+.4f} ({float(t.pnl_percent or 0):+.2f}%)")
        
        triggered = pos.status == 'CLOSED'
        print(f"\n  {test_type.upper()}: {'✅ TRIGGERED' if triggered else '❌ NOT TRIGGERED'}")
        return pos
    else:
        print(f"  ❌ No triggers — unexpected")
        return None

# Run SL test
pos1 = run_test('sl')

# Run TP test
pos2 = run_test('tp')

print(f"\n{'='*60}")
print(f"  FINAL SUMMARY")
print(f"{'='*60}")
print(f"  SL test: {'✅' if pos1 and pos1.status=='CLOSED' else '❌'}")
print(f"  TP test: {'✅' if pos2 and pos2.status=='CLOSED' else '❌'}")
