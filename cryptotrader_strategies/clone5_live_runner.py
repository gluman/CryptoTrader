#!/usr/bin/env python3
"""
Clone5 Live Runner — генерация сигналов и отправка в Bybit (через CryptoTrader ExecutionAgent).

Использование:
  python clone5_live_runner.py --once    # один проход, не блокировать
  python clone5_live_runner.py --loop 60 # в цикле, каждые 60 сек

Требования:
  - CryptoTrader ExecutionAgent уже в main.py (его нужно перезапустить)
  - Стратегия 'clone5_market_maker' зарегистрирована в DecisionAgent
"""
import os
import sys
import time
import json
import argparse
import signal
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, '/home/andy/CryptoTrader')

from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import psycopg2
import pandas as pd
import ccxt
from cryptotrader_strategies.clone_5_v7 import Clone5V7Strategy

DB = dict(
    host="192.168.0.149", port=5432, database="cryptotrader",
    user="cryptotrader", password=os.environ["POSTGRES_PASSWORD"],
)

# Пары Clone5 v7 (топ по FULL multi-pair backtest без BSBUSDT)
SYMBOLS = ["SUIUSDT", "NEARUSDT", "SOLUSDT", "LITUSDT", "WLDUSDT", "TONUSDT", "DOGEUSDT", "ADAUSDT"]
TF = "5m"
TIMEFRAME_MIN = 5

# Exchange для live цен
exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}}, )
exchange.load_markets()

# Position size (динамический, обновляется compound_engine --rebalance)
def get_pos_usdt() -> float:
    try:
        state = json.loads(Path('/home/andy/CryptoTrader/compound_state.json').read_text())
        return float(state.get('current_pos_usdt', 5.0))
    except Exception:
        return 5.0

POS_USDT = get_pos_usdt()  # будет пересчитан в scan_once()


def get_db():
    return psycopg2.connect(**DB)


def get_ohlcv(symbol: str, lookback_bars: int = 200) -> pd.DataFrame:
    """Получить последние N баров 5m по символу."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT timestamp, open, high, low, close, volume
            FROM ohlcv_raw
            WHERE exchange='bybit' AND timeframe=%s AND symbol=%s
            ORDER BY timestamp DESC
            LIMIT %s
        """, (TF, symbol, lookback_bars))
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = df.iloc[::-1].reset_index(drop=True)  # reverse to ascending
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        df.set_index('timestamp', inplace=True)
        df = df.astype(float)
        return df
    finally:
        conn.close()


def open_position(symbol: str, side: str, sl_pct: float, tp_pct: float,
                  score: float, reasoning: str, details: dict) -> int:
    """Создать запись в strategy_signals (для ExecutionAgent)."""
    pos_usdt = get_pos_usdt()  # dynamic from compound engine
    conn = get_db()
    try:
        cur = conn.cursor()
        # Получаем текущую цену
        ticker = exchange.fetch_ticker(f"{symbol}")
        entry_price = ticker['last']
        if side == 'LONG':
            sl_price = entry_price * (1 - sl_pct / 100)
            tp_price = entry_price * (1 + tp_pct / 100)
        else:  # SHORT
            sl_price = entry_price * (1 + sl_pct / 100)
            tp_price = entry_price * (1 - tp_pct / 100)
        # Записываем сигнал
        cur.execute("""
            INSERT INTO strategy_signals
              (strategy_name, symbol, side, action, score, confidence,
               entry_price, sl_price, tp_price, sl_pct, tp_pct,
               position_usdt, leverage, details, created_at, status)
            VALUES (%s, %s, %s, 'OPEN', %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, NOW(), 'pending')
            RETURNING id
        """, (
            'clone5_v7_market_maker_martingale', symbol, side, score, score,
            entry_price, sl_price, tp_price, sl_pct, tp_pct,
            pos_usdt, json.dumps(details, default=str),
        ))
        sig_id = cur.fetchone()[0]
        conn.commit()
        print(f"  ✓ OPEN {side} {symbol} @ ${entry_price:.4f}  SL=${sl_price:.4f} TP=${tp_price:.4f}  score={score:.2f}  pos=${pos_usdt:.2f}", flush=True)
        return sig_id
    finally:
        conn.close()


def has_open_position(symbol: str) -> bool:
    """Проверить, есть ли уже открытая позиция по символу."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM positions
            WHERE symbol=%s AND status='open'
        """, (symbol,))
        n = cur.fetchone()[0]
        return n > 0
    finally:
        conn.close()


def scan_once() -> int:
    """Один проход: проверить все пары, сгенерировать сигналы. Возвращает кол-во сигналов."""
    strategy = Clone5V7Strategy()
    signals = 0
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Clone5 scan: {len(SYMBOLS)} pairs", flush=True)
    for sym in SYMBOLS:
        if has_open_position(sym):
            print(f"  ⊘ {sym}: position already open, skip", flush=True)
            continue
        df = get_ohlcv(sym, lookback_bars=300)
        if df.empty or len(df) < 100:
            print(f"  ⊘ {sym}: no data ({len(df)} bars)", flush=True)
            continue
        decision = strategy.decide(df, sym)
        sig = decision.get('signal', 'HOLD')
        if sig == 'HOLD':
            continue
        if decision.get('confidence', 0) < 0.50:
            continue
        # Open!
        side = decision.get('side', 'LONG' if sig == 'BUY' else 'SHORT')
        sl_pct = decision.get('stop_loss_pct', 0.5)
        tp_pct = decision.get('take_profit_pct', 2.0)
        score = decision.get('confidence', 0.5)
        details = decision.get('details', {})
        details['reasoning'] = decision.get('reasoning', '')
        open_position(sym, side, sl_pct, tp_pct, score, decision.get('reasoning', ''), details)
        signals += 1
    return signals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Single pass')
    parser.add_argument('--loop', type=int, default=0, help='Loop interval in seconds (0 = run once)')
    args = parser.parse_args()
    if args.once or args.loop == 0:
        n = scan_once()
        print(f"\nSignals generated: {n}", flush=True)
        return
    print(f"Clone5 Live Runner — loop every {args.loop}s", flush=True)
    stop = False
    def handle_sig(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)
    while not stop:
        try:
            n = scan_once()
            if n == 0:
                # No signals — sleep most of the time
                time.sleep(args.loop)
            else:
                # After signal, wait a bit before next scan
                time.sleep(min(args.loop, 30))
        except Exception as e:
            print(f"  ! error: {e}", flush=True)
            time.sleep(60)


if __name__ == "__main__":
    main()
