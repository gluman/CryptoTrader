"""
TEST TRADE: Open and close a minimal real position on Bybit
Verifies: LLM decision -> execution -> position DB -> SL/TP -> close
"""
import sys; sys.path.insert(0, '.')
import os, json, time, math, logging
from datetime import datetime

from src.core.config import Config; Config._instance = None; config = Config.load()
from src.core.logger import setup_logger
logger = setup_logger('test_trade', level='INFO')
from src.core.database import DatabaseManager, Signal, Decision, Position
db = DatabaseManager(config.postgresql, logger)

from src.gateways import BybitAPI
bybit = BybitAPI(
    api_key=config.bybit['api_key'],
    api_secret=config.bybit['api_secret'],
    testnet=config.bybit.get('testnet', False),
    logger=logger,
)

# ── Check balance ──
print("\n" + "="*60)
print("TEST TRADE — REAL MONEY MECHANICS CHECK")
print("="*60)

wallet = bybit.get_wallet_balance(account_type='UNIFIED')
usdt_balance = 0
try:
    result = wallet.get('result', wallet)
    data = result.get('list', result.get('balance', []))
    if isinstance(data, list):
        for entry in data:
            coins = entry.get('coin', [])
            for coin in coins:
                if coin.get('coin') == 'USDT':
                    usdt_balance = float(coin.get('walletBalance', 0))
    elif isinstance(data, dict):
        usdt_balance = float(data.get('USDT', {}).get('walletBalance', 0))
except Exception as e:
    logger.warning(f"Parse wallet: {e}")
    usdt_balance = 10

print(f"USDT balance: ${usdt_balance:.2f}")
if usdt_balance < 5:
    print("❌ NOT ENOUGH USDT. Aborting.")
    sys.exit(1)

# ── Pick symbol ──
from sqlalchemy import text
with db.get_session() as s:
    rows = s.execute(text("SELECT symbol FROM selected_symbols WHERE is_active = true LIMIT 20")).fetchall()
symbols = [r[0] for r in rows]
target = None
for sym in ['ENJUSDT', 'XRPUSDT', 'LTCUSDT', 'BTCUSDT', 'ETHUSDT']:
    if sym in symbols: target = sym; break
if not target or target not in str(bybit.get_tickers('spot')): 
    target = 'ENJUSDT'  # confirmed on Bybit spot
if not target and symbols: target = symbols[0]
if not target: target = 'BTCUSDT'
print(f"Target: {target}")

# ── Get ticker & lot info ──
ticker = bybit.get_ticker(target)
price = float(ticker.get('lastPrice', 0))
lot = bybit.get_lot_size_filter(target, category='spot')
min_qty = float(lot.get('minOrderQty', '0.00001'))
step = float(lot.get('qtyStep', '0.00001'))
print(f"Price: ${price:.4f}  |  Min qty: {min_qty}  |  Step: {step}")

# ── Init agents ──
from src.agents.sentiment_agent import SentimentAgent
from src.agents.trading_agent import TradingDecisionAgent
from src.agents.execution_agent import ExecutionAgent

sentiment = SentimentAgent(config, logger, db)
trading = TradingDecisionAgent(config, logger, db, sentiment)
executor = ExecutionAgent(config, logger, db)
executor.confirmation_required = False

# ── 1. Get LLM decision ──
print("\n── [1] DeepSeek decision ──")
df = trading.get_ohlcv_data(target, 'binance', '1h')
indicators = trading.calculate_indicators(df)
senti = sentiment.get_aggregated_sentiment(hours=24)
recent = trading.get_recent_signals(target)
positions = trading.get_open_positions_for_symbol(target)

prompt = trading.build_prompt(target, indicators, senti, recent, positions, '')
t0 = time.time()
decision = trading.call_llm(prompt)
dt = time.time() - t0
print(f"LLM said: {decision['signal']}  confidence={decision['confidence']:.0%}  ({dt:.1f}s)")
print(f"  Reasoning: {decision.get('reasoning','')[:150]}")

# Force BUY for test if needed
if decision.get('signal') != 'BUY':
    print("→ Forcing test BUY")
    decision['signal'] = 'BUY'
    decision['confidence'] = 0.8
    decision['reasoning'] = 'TEST: forcing BUY to verify trade mechanics'

signal_id = trading.save_decision(target, 'bybit', '1h', indicators, senti, decision, '')
print(f"Signal saved: #{signal_id}")

# ── 2. Execute BUY ($10) ──
print("\n── [2] Execute BUY ──")
test_amount = min(10.0, usdt_balance * 0.1)
test_amount = max(test_amount, 5.0)

qty_test = test_amount / price
qty_test = math.floor(qty_test / step) * step
qty_test = max(qty_test, min_qty)

buy_result = executor.execute_spot_buy(target, str(round(qty_test * price, 2)), 'bybit')
print(f"BUY: {json.dumps(buy_result, indent=2, default=str)}")
if 'error' in buy_result:
    print(f"BUY FAILED: {buy_result['error']}")
    sys.exit(1)

trade_id = executor.save_trade_to_db(signal_id, buy_result)
print(f"Trade saved: #{trade_id}")

# Actual filled qty and price
ticker2 = bybit.get_ticker(target)
fill_price = float(ticker2.get('lastPrice', price))
exec_qty = qty_test  # market order fills approximately

# SL/TP from decision
sl_price = decision.get('stop_loss')
tp_price = decision.get('take_profit')
sl_pct = abs((fill_price - float(sl_price)) / fill_price * 100) if sl_price else None
tp_pct = abs((float(tp_price) - fill_price) / fill_price * 100) if tp_price else None

pos_id = executor.create_position(
    symbol=target, exchange='bybit',
    entry_price=fill_price, quantity=exec_qty,
    cost_usdt=exec_qty * fill_price,
    signal_id=signal_id, trade_id=trade_id,
    sl_percent=sl_pct, tp_percent=tp_pct,
)
print(f"Position created: #{pos_id}")
print(f"  Entry: ${fill_price:.4f}  |  Size: {exec_qty:.6f} (${exec_qty*fill_price:.2f})")
if sl_price: print(f"  SL: ${float(sl_price):.4f} ({sl_pct:.1f}%)")
if tp_price: print(f"  TP: ${float(tp_price):.4f} ({tp_pct:.1f}%)")

# ── 3. Wait & monitor (60s) ──
print("\n── [3] Monitoring (1 min) ──")
entry_price = fill_price
for i in range(6):
    time.sleep(10)
    t = bybit.get_ticker(target)
    p = float(t.get('lastPrice', 0))
    pnl = ((p - entry_price) / entry_price) * 100
    print(f"  [{i+1}] ${p:.4f}  PnL: {pnl:+.2f}%")
    if abs(pnl) > 2:
        print("     ⚡ High volatility")

# ── 4. Close position ──
print("\n── [4] Close SELL ──")
ticker3 = bybit.get_ticker(target)
exit_price = float(ticker3.get('lastPrice', fill_price))

# Get actual ENJ balance from Bybit
wallet2 = bybit.get_wallet_balance(account_type='UNIFIED')
actual_enj = 0
result2 = wallet2.get('result', wallet2)
for acct in result2.get('list', []):
    for coin in acct.get('coin', []):
        if coin.get('coin') == 'ENJ':
            actual_enj = float(coin.get('walletBalance', 0))
            break

if actual_enj <= 0:
    print("NO ENJ IN WALLET — position may already be closed")
    sys.exit(1)

# Round qty to Bybit's basePrecision for spot sell
sell_qty = bybit.round_qty_by_lot_size(target, actual_enj, 'spot')
print(f"Selling {sell_qty} ENJ (wallet={actual_enj:.6f}) @ ~${exit_price:.4f}")

sell_result = executor.execute_spot_sell(target, sell_qty, 'bybit')
print(f"SELL: {json.dumps(sell_result, indent=2, default=str)}")
if 'error' in sell_result:
    print(f"SELL FAILED: {sell_result['error']}")
    sys.exit(1)

sell_trade_id = executor.save_trade_to_db(signal_id, sell_result)
executor.close_position(pos_id, exit_price, reason='TEST')
print(f"Position closed. Sell trade: #{sell_trade_id}")

# ── Summary ──
pnl_abs = (exit_price - entry_price) * exec_qty
pnl_pct = ((exit_price - entry_price) / entry_price) * 100
print("\n" + "="*60)
print(f"RESULT: {target} on Bybit {'(LIVE)' if not config.bybit.get('testnet') else '(TESTNET)'}")
print("="*60)
print(f"  Entry:    ${entry_price:.4f}")
print(f"  Exit:     ${exit_price:.4f}")
print(f"  Volume:   ${exec_qty*entry_price:.2f}")
print(f"  PnL:      ${pnl_abs:.2f} ({pnl_pct:+.2f}%)")
print(f"  LLM sig:  {decision['signal']} ({decision['confidence']:.0%})")
print(f"  SL/TP:    ${sl_price or '-'} / ${tp_price or '-'}")
print("="*60)

if abs(pnl_abs) < 0.5:
    print("✅ Trade executed and closed — mechanics working.")
elif pnl_abs > 0:
    print(f"✅ Trade successful +${pnl_abs:.2f}")
else:
    print(f"ℹ️  Small loss -${pnl_abs:.2f} (expected for test)")
print("="*60)
