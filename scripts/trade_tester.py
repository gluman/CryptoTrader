#!/usr/bin/env python3
"""
Trade Tester — CLI утилита для тестирования механик торговли на Bybit spot.
Режимы:
  buy [--symbol SYM] [--amount $]       — BUY market
  close [--position_id ID]              — SELL close
  sl [--symbol SYM] [--amount $]        — BUY + SL (0.5%)
  tp [--symbol SYM] [--amount $]        — BUY + TP (0.5%)
  full [--symbol SYM] [--amount $]      — BUY → wait → SELL close
  pipeline [--symbol SYM]               — Реальный pipeline (DeepSeek + исполнение)
  balance                                — Показать баланс
  positions                              — Показать открытые позиции
  sync                                   — Синхронизировать позиции с биржей
"""
import sys; sys.path.insert(0, '.')
import os, json, time, math, argparse, logging
from datetime import datetime
from typing import Optional

from src.core.config import Config; Config._instance = None; config = Config.load()
from src.core.logger import setup_logger
logger = setup_logger('trade_tester', level='INFO')
from src.core.database import DatabaseManager, Signal, Decision, Position, SelectedSymbol
from src.gateways import BybitAPI

# ── Init ──
db = DatabaseManager(config.postgresql, logger)
bybit = BybitAPI(
    api_key=config.bybit['api_key'],
    api_secret=config.bybit['api_secret'],
    testnet=config.bybit.get('testnet', False),
    logger=logger,
)

from src.agents.sentiment_agent import SentimentAgent
from src.agents.trading_agent import TradingDecisionAgent
from src.agents.execution_agent import ExecutionAgent

sentiment = SentimentAgent(config, logger, db)
trading = TradingDecisionAgent(config, logger, db, sentiment)
executor = ExecutionAgent(config, logger, db)
executor.confirmation_required = False

# ── Helpers ──
def get_usdt_balance() -> float:
    wallet = bybit.get_wallet_balance(account_type='UNIFIED')
    try:
        result = wallet.get('result', wallet)
        for acct in result.get('list', []):
            for coin in acct.get('coin', []):
                if coin.get('coin') == 'USDT':
                    return float(coin.get('walletBalance', 0))
    except: pass
    return 0

def get_coin_balance(coin: str) -> float:
    wallet = bybit.get_wallet_balance(account_type='UNIFIED')
    try:
        result = wallet.get('result', wallet)
        for acct in result.get('list', []):
            for c in acct.get('coin', []):
                if c.get('coin') == coin:
                    return float(c.get('walletBalance', 0))
    except: pass
    return 0

def get_symbol_data() -> tuple:
    """Pick a testable symbol and get its price/lot info"""
    with db.get_session() as s:
        rows = s.query(SelectedSymbol.symbol).filter(SelectedSymbol.is_active == True).limit(20).all()
    symbols = [r[0] for r in rows]
    
    pref = ['ENJUSDT', 'XRPUSDT', 'LTCUSDT', 'ADAUSDT', 'DOGEUSDT', 'MATICUSDT', 'SOLUSDT']
    target = None
    for p in pref:
        if p in symbols:
            target = p
            break
    if not target:
        target = symbols[0] if symbols else 'ENJUSDT'
    
    # Verify on Bybit spot
    try:
        bybit.get_ticker(target)
    except:
        target = 'ENJUSDT'
    
    ticker = bybit.get_ticker(target)
    price = float(ticker.get('lastPrice', 0))
    lot = bybit.get_lot_size_filter(target, category='spot')
    min_qty = float(lot.get('minOrderQty', '0.00001'))
    step = float(lot.get('qtyStep', '0.00001'))
    
    return target, price, min_qty, step

def calc_qty(amount: float, price: float, step: float, min_qty: float) -> float:
    qty = math.floor(amount / price / step) * step
    return max(qty, min_qty)

def print_header(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ── Handlers ──
def cmd_balance(args):
    print_header("BALANCE")
    print(f"  USDT: ${get_usdt_balance():.2f}")
    ticker = bybit.get_ticker('ENJUSDT')
    if ticker:
        print(f"  ENJUSDT: ${float(ticker.get('lastPrice',0)):.4f}")

def cmd_positions(args):
    print_header("OPEN POSITIONS")
    opens = executor.get_all_open_positions()
    if not opens:
        print("  No open positions")
        return
    for p in opens:
        print(f"  #{p['id']} {p['symbol']}: entry=${float(p['entry_price']):.4f} qty={float(p['quantity']):.6f}")
        print(f"    SL=${float(p['stop_loss']):.4f} TP=${float(p['take_profit']):.4f}" if p['stop_loss'] else "    SL=None")

def cmd_sync(args):
    print_header("SYNC POSITIONS")
    r = executor.sync_positions_from_exchange('bybit')
    print(f"  created={r.get('created',0)} updated={r.get('updated',0)} closed={r.get('closed',0)} synced={r.get('synced',0)}")

def cmd_buy(args):
    target, price, min_qty, step = get_symbol_data()
    amount = args.amount or min(10.0, get_usdt_balance() * 0.2)
    
    print_header(f"BUY {target} (${amount:.2f})")
    print(f"  Market: ${price:.4f}")
    
    qty = calc_qty(amount, price, step, min_qty)
    cost = qty * price
    
    # Get LLM decision for SL/TP reference
    df = trading.get_ohlcv_data(target, 'binance', '1h')
    indicators = trading.calculate_indicators(df)
    senti = sentiment.get_aggregated_sentiment(hours=24)
    recent = trading.get_recent_signals(target)
    positions = trading.get_open_positions_for_symbol(target)
    prompt = trading.build_prompt(target, indicators, senti, recent, positions, '')
    decision = trading.call_llm(prompt)
    
    print(f"  LLM: {decision['signal']} (conf={decision['confidence']:.0%})")
    
    buy_result = executor.execute_spot_buy(target, str(round(cost, 2)), 'bybit')
    if 'error' in buy_result:
        print(f"  BUY FAILED: {buy_result['error']}")
        return
    
    signal_id = trading.save_decision(target, 'bybit', '1h', indicators, senti, decision, '')
    trade_id = executor.save_trade_to_db(signal_id, buy_result)
    print(f"  Signal: #{signal_id}  Trade: #{trade_id}")
    
    # Create position with SL/TP from LLM
    ticker2 = bybit.get_ticker(target)
    fill_price = float(ticker2.get('lastPrice', price))
    
    sl_price = decision.get('stop_loss')
    tp_price = decision.get('take_profit')
    sl_pct = abs((fill_price - float(sl_price)) / fill_price * 100) if sl_price else None
    tp_pct = abs((float(tp_price) - fill_price) / fill_price * 100) if tp_price else None
    
    pos_id = executor.create_position(
        symbol=target, exchange='bybit',
        entry_price=fill_price, quantity=qty,
        cost_usdt=cost, signal_id=signal_id, trade_id=trade_id,
        sl_percent=sl_pct, tp_percent=tp_pct,
    )
    print(f"  Position: #{pos_id} @ ${fill_price:.4f}")
    print(f"  SL: ${float(sl_price):.4f}" if sl_price else "  SL: default")
    print(f"  TP: ${float(tp_price):.4f}" if tp_price else "  TP: default")

def cmd_full(args):
    """BUY → monitor → SELL close"""
    target, price, min_qty, step = get_symbol_data()
    amount = args.amount or min(10.0, get_usdt_balance() * 0.2)
    
    print_header(f"FULL CYCLE: {target} (${amount:.2f})")
    print(f"  Market: ${price:.4f}")
    
    qty = calc_qty(amount, price, step, min_qty)
    cost = qty * price
    print(f"  Qty: {qty:.6f} (${cost:.2f})")
    
    # ---- BUY ----
    buy_result = executor.execute_spot_buy(target, str(round(cost, 2)), 'bybit')
    if 'error' in buy_result:
        print(f"BUY FAILED: {buy_result['error']}")
        return
    
    # Save minimal signal/decision
    signal_id = trading.save_decision(target, 'bybit', '1h', {}, {}, {'signal': 'BUY', 'confidence': 0.8, 'reasoning': 'TEST full cycle'}, '')
    trade_id = executor.save_trade_to_db(signal_id, buy_result)
    
    ticker2 = bybit.get_ticker(target)
    fill_price = float(ticker2.get('lastPrice', price))
    
    pos_id = executor.create_position(
        symbol=target, exchange='bybit',
        entry_price=fill_price, quantity=qty,
        cost_usdt=qty * fill_price,
        signal_id=signal_id, trade_id=trade_id,
        sl_percent=2.0, tp_percent=4.0,
    )
    entry_price = fill_price
    print(f"  BUY executed: #{trade_id} @ ${entry_price:.4f}")
    print(f"  Position: #{pos_id}  SL=2% TP=4%")
    
    # ---- MONITOR ----
    print(f"\n── Monitoring (60s) ──")
    for i in range(6):
        time.sleep(10)
        t = bybit.get_ticker(target)
        p = float(t.get('lastPrice', 0))
        pnl = ((p - entry_price) / entry_price) * 100
        print(f"  [{i+1}] ${p:.4f}  PnL: {pnl:+.2f}%")
        
        # Check SL/TP
        triggers = executor.check_stop_loss_take_profit(target, p)
        if triggers:
            for tr in triggers:
                print(f"  ⚡ {tr['type']} TRIGGERED!")
            break
    
    ticker3 = bybit.get_ticker(target)
    exit_price = float(ticker3.get('lastPrice', fill_price))
    
    # ---- SELL ----
    coin_name = target.replace('USDT', '')
    bal = get_coin_balance(coin_name)
    if bal > 0:
        sell_qty = bybit.round_qty_by_lot_size(target, bal, 'spot')
        print(f"\n── Selling {sell_qty} {coin_name} @ ~${exit_price:.4f} ──")
        sell_result = executor.execute_spot_sell(target, sell_qty, 'bybit')
        if 'error' in sell_result:
            print(f"SELL FAILED: {sell_result['error']}")
        else:
            sell_trade = executor.save_trade_to_db(signal_id, sell_result)
            executor.close_position(pos_id, exit_price, reason='TEST_FULL')
            print(f"  SELL executed: #{sell_trade}")
    else:
        print(f"\n  No {coin_name} in wallet — already closed?")
    
    # Summary
    pnl_abs = (exit_price - entry_price) * qty
    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
    print(f"\n{'='*60}")
    print(f"RESULT: PnL ${pnl_abs:.2f} ({pnl_pct:+.2f}%)")
    print(f"{'='*60}")

def cmd_sl(args):
    """BUY with tight SL — check if SL triggers"""
    target, price, min_qty, step = get_symbol_data()
    amount = args.amount or min(5.0, get_usdt_balance() * 0.1)
    
    print_header(f"SL TEST: {target} (${amount:.2f})")
    print(f"  Market: ${price:.4f}")
    
    qty = calc_qty(amount, price, step, min_qty)
    cost = qty * price
    print(f"  Qty: {qty:.6f} (${cost:.2f})")
    
    # BUY
    buy_result = executor.execute_spot_buy(target, str(round(cost, 2)), 'bybit')
    if 'error' in buy_result:
        print(f"BUY FAILED: {buy_result['error']}")
        return
    
    signal_id = trading.save_decision(target, 'bybit', '1h', {}, {}, {'signal': 'BUY', 'confidence': 0.8, 'reasoning': 'TEST SL'}, '')
    trade_id = executor.save_trade_to_db(signal_id, buy_result)
    
    ticker2 = bybit.get_ticker(target)
    fill_price = float(ticker2.get('lastPrice', price))
    
    # SL at 0.5% below entry
    sl_pct = 0.5
    sl_price = fill_price * (1 - sl_pct / 100)
    pos_id = executor.create_position(
        symbol=target, exchange='bybit',
        entry_price=fill_price, quantity=qty,
        cost_usdt=qty * fill_price,
        signal_id=signal_id, trade_id=trade_id,
        sl_percent=sl_pct, tp_percent=None,
    )
    entry_price = fill_price
    print(f"  BUY @ ${entry_price:.4f}  |  SL @ ${sl_price:.4f} (0.5%)")
    print(f"  Position: #{pos_id}")
    
    # Monitor
    print(f"\n── Monitoring (90s) for SL trigger ──")
    sl_triggered = False
    for i in range(9):
        time.sleep(10)
        p = float(bybit.get_ticker(target).get('lastPrice', 0))
        pnl = ((p - entry_price) / entry_price) * 100
        triggers = executor.check_stop_loss_take_profit(target, p)
        if triggers:
            for tr in triggers:
                print(f"  [{i+1}] ${p:.4f} PnL: {pnl:+.2f}% ⚡ {tr['type']} TRIGGERED at ${tr['trigger_price']:.4f}")
            sl_triggered = True
            break
        print(f"  [{i+1}] ${p:.4f} PnL: {pnl:+.2f}%  (SL=${sl_price:.4f})")
    
    # Close remaining
    coin_name = target.replace('USDT', '')
    bal = get_coin_balance(coin_name)
    if bal > 0:
        exit_p = float(bybit.get_ticker(target).get('lastPrice', price))
        sell_qty = bybit.round_qty_by_lot_size(target, bal, 'spot')
        print(f"\n  Closing {sell_qty} {coin_name} @ ${exit_p:.4f}...")
        sell_result = executor.execute_spot_sell(target, sell_qty, 'bybit')
        if 'error' not in sell_result:
            executor.save_trade_to_db(signal_id, sell_result)
            executor.close_position(pos_id, exit_p, reason='TEST_SL_CLOSE')
            print(f"  → Closed")
        else:
            print(f"  → Close failed: {sell_result['error']}")
    
    if sl_triggered:
        print(f"\n✅ SL test PASSED — stop loss triggered correctly")
    else:
        print(f"\nℹ️  SL did not trigger (normal volatility), position closed manually")

def cmd_tp(args):
    """BUY with tight TP — check if TP triggers"""
    target, price, min_qty, step = get_symbol_data()
    amount = args.amount or min(5.0, get_usdt_balance() * 0.1)
    
    print_header(f"TP TEST: {target} (${amount:.2f})")
    print(f"  Market: ${price:.4f}")
    
    qty = calc_qty(amount, price, step, min_qty)
    cost = qty * price
    
    # BUY
    buy_result = executor.execute_spot_buy(target, str(round(cost, 2)), 'bybit')
    if 'error' in buy_result:
        print(f"BUY FAILED: {buy_result['error']}")
        return
    
    signal_id = trading.save_decision(target, 'bybit', '1h', {}, {}, {'signal': 'BUY', 'confidence': 0.8, 'reasoning': 'TEST TP'}, '')
    trade_id = executor.save_trade_to_db(signal_id, buy_result)
    
    ticker2 = bybit.get_ticker(target)
    fill_price = float(ticker2.get('lastPrice', price))
    
    # TP at 0.5% above entry (tight)
    tp_pct = 0.5
    tp_price = fill_price * (1 + tp_pct / 100)
    pos_id = executor.create_position(
        symbol=target, exchange='bybit',
        entry_price=fill_price, quantity=qty,
        cost_usdt=qty * fill_price,
        signal_id=signal_id, trade_id=trade_id,
        sl_percent=None, tp_percent=tp_pct,
    )
    entry_price = fill_price
    print(f"  BUY @ ${entry_price:.4f}  |  TP @ ${tp_price:.4f} (0.5%)")
    print(f"  Position: #{pos_id}")
    
    # Monitor
    print(f"\n── Monitoring (90s) for TP trigger ──")
    tp_triggered = False
    for i in range(9):
        time.sleep(10)
        p = float(bybit.get_ticker(target).get('lastPrice', 0))
        pnl = ((p - entry_price) / entry_price) * 100
        triggers = executor.check_stop_loss_take_profit(target, p)
        if triggers:
            for tr in triggers:
                print(f"  [{i+1}] ${p:.4f} PnL: {pnl:+.2f}% ⚡ {tr['type']} TRIGGERED at ${tr['trigger_price']:.4f}")
            tp_triggered = True
            break
        print(f"  [{i+1}] ${p:.4f} PnL: {pnl:+.2f}%  (TP=${tp_price:.4f})")
    
    # Close remaining
    coin_name = target.replace('USDT', '')
    bal = get_coin_balance(coin_name)
    if bal > 0:
        exit_p = float(bybit.get_ticker(target).get('lastPrice', price))
        sell_qty = bybit.round_qty_by_lot_size(target, bal, 'spot')
        print(f"\n  Closing {sell_qty} {coin_name} @ ${exit_p:.4f}...")
        sell_result = executor.execute_spot_sell(target, sell_qty, 'bybit')
        if 'error' not in sell_result:
            executor.save_trade_to_db(signal_id, sell_result)
            executor.close_position(pos_id, exit_p, reason='TEST_TP_CLOSE')
            print(f"  → Closed")
        else:
            print(f"  → Close failed: {sell_result['error']}")
    
    if tp_triggered:
        print(f"\n✅ TP test PASSED — take profit triggered correctly")
    else:
        print(f"\nℹ️  TP did not trigger (normal volatility), position closed manually")

def cmd_pipeline(args):
    """Full pipeline: DeepSeek → execute → monitor → close"""
    target, price, min_qty, step = get_symbol_data()
    amount = args.amount or min(10.0, get_usdt_balance() * 0.2)
    
    print_header(f"PIPELINE TEST: {target} (via DeepSeek)")
    
    # 1. DeepSeek decision with REAL indicators
    df = trading.get_ohlcv_data(target, 'binance', '1h')
    if df.empty:
        print("NO DATA for DeepSeek")
        return
    indicators = trading.calculate_indicators(df)
    senti = sentiment.get_aggregated_sentiment(hours=24)
    recent = trading.get_recent_signals(target)
    open_pos = trading.get_open_positions_for_symbol(target)
    
    print(f"  Indicators: price={indicators.get('price',0):.4f} RSI={indicators.get('rsi_14',0):.1f} MACD={indicators.get('macd',0):.6f}")
    print(f"  Sentiment: {senti.get('avg_sentiment',0):.3f}")
    
    prompt = trading.build_prompt(target, indicators, senti, recent, open_pos, '')
    t0 = time.time()
    decision = trading.call_llm(prompt)
    dt = time.time()-t0
    
    print(f"\n  DeepSeek ({dt:.1f}s): {decision['signal']} (conf={decision['confidence']:.0%})")
    print(f"  Reasoning: {decision.get('reasoning','')[:200]}")
    print(f"  SL: {decision.get('stop_loss')}  TP: {decision.get('take_profit')}")
    
    signal_id = trading.save_decision(target, 'bybit', '1h', indicators, senti, decision, '')
    print(f"  Signal: #{signal_id}")
    
    # 2. Execute if BUY/SELL
    if decision['signal'] == 'BUY':
        qty = calc_qty(amount, price, step, min_qty)
        cost = qty * price
        print(f"\n── Executing BUY (${cost:.2f}) ──")
        
        buy_result = executor.execute_spot_buy(target, str(round(cost, 2)), 'bybit')
        if 'error' in buy_result:
            print(f"BUY FAILED: {buy_result['error']}")
            return
        
        trade_id = executor.save_trade_to_db(signal_id, buy_result)
        ticker2 = bybit.get_ticker(target)
        fill_price = float(ticker2.get('lastPrice', price))
        
        sl_price = decision.get('stop_loss')
        tp_price = decision.get('take_profit')
        sl_pct = abs((fill_price - float(sl_price)) / fill_price * 100) if sl_price else None
        tp_pct = abs((float(tp_price) - fill_price) / fill_price * 100) if tp_price else None
        
        pos_id = executor.create_position(
            symbol=target, exchange='bybit',
            entry_price=fill_price, quantity=qty,
            cost_usdt=cost, signal_id=signal_id, trade_id=trade_id,
            sl_percent=sl_pct, tp_percent=tp_pct,
        )
        print(f"  Position #{pos_id}: entry=${fill_price:.4f} qty={qty:.6f}")
        print(f"  SL=${float(sl_price):.4f}" if sl_price else "  SL=default (2%)")
        print(f"  TP=${float(tp_price):.4f}" if tp_price else "  TP=default (4%)")
        
        # Monitor & close after 60s
        print(f"\n── Monitoring (60s) ──")
        entry_p = fill_price
        for i in range(6):
            time.sleep(10)
            p = float(bybit.get_ticker(target).get('lastPrice', 0))
            pnl = ((p - entry_p) / entry_p) * 100
            triggers = executor.check_stop_loss_take_profit(target, p)
            if triggers:
                print(f"  [{i+1}] ${p:.4f} PnL: {pnl:+.2f}% ⚡ {triggers[0]['type']}")
                break
            print(f"  [{i+1}] ${p:.4f} PnL: {pnl:+.2f}%")
        
        # Close
        exit_p = float(bybit.get_ticker(target).get('lastPrice', fill_price))
        coin_name = target.replace('USDT', '')
        bal = get_coin_balance(coin_name)
        if bal > 0:
            sell_qty = bybit.round_qty_by_lot_size(target, bal, 'spot')
            print(f"\n── Closing {sell_qty} {coin_name} @ ${exit_p:.4f} ──")
            sell_result = executor.execute_spot_sell(target, sell_qty, 'bybit')
            if 'error' not in sell_result:
                executor.save_trade_to_db(signal_id, sell_result)
                executor.close_position(pos_id, exit_p, reason='TEST_PIPELINE')
        else:
            print(f"\n  No {coin_name} left — auto-closed?")
        
        pnl = (exit_p - entry_p) * qty
        print(f"\n  PnL: ${pnl:.2f} ({(pnl/entry_p/qty*100 if entry_p*qty else 0):+.2f}%)")
        
    elif decision['signal'] == 'SELL':
        print("\n  SELL signal — would short, skipping (spot only)")
    else:
        print("\n  HOLD — forcing test BUY")
        # Force buy to test execution anyway
        qty = calc_qty(5.0, price, step, min_qty)
        cost = qty * price
        buy_result = executor.execute_spot_buy(target, str(round(cost, 2)), 'bybit')
        if 'error' not in buy_result:
            trade_id = executor.save_trade_to_db(signal_id, buy_result)
            pos_id = executor.create_position(
                symbol=target, exchange='bybit',
                entry_price=price, quantity=qty,
                cost_usdt=cost, signal_id=signal_id, trade_id=trade_id,
            )
            print(f"  FORCED BUY @ ${price:.4f} — position #{pos_id}")
            # Close immediately
            time.sleep(5)
            exit_p = float(bybit.get_ticker(target).get('lastPrice', price))
            coin_name = target.replace('USDT', '')
            bal = get_coin_balance(coin_name)
            if bal > 0:
                sell_qty = bybit.round_qty_by_lot_size(target, bal, 'spot')
                executor.execute_spot_sell(target, sell_qty, 'bybit')
                executor.close_position(pos_id, exit_p, reason='HOLD_FORCED')
                print(f"  Closed @ ${exit_p:.4f}")

# ── CLI ──
parser = argparse.ArgumentParser(description='Trade Tester')
parser.add_argument('mode', choices=['buy','close','sl','tp','full','pipeline','balance','positions','sync'],
                    help='Режим теста')
parser.add_argument('--symbol', '-s', help='Символ (ENJUSDT, XRPUSDT...)')
parser.add_argument('--amount', '-a', type=float, help='Сумма в USDT')
parser.add_argument('--position_id', '-p', type=int, help='ID позиции для close')

args = parser.parse_args()

# Route
handlers = {
    'balance': cmd_balance,
    'positions': cmd_positions,
    'sync': cmd_sync,
    'buy': cmd_buy,
    'full': cmd_full,
    'sl': cmd_sl,
    'tp': cmd_tp,
    'pipeline': cmd_pipeline,
}

handlers[args.mode](args)
