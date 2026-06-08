#!/usr/bin/env python3
"""
Multi-strategy live runner — сканирует v2/v6/v7a параллельно в одной cron-итерации.

Каждая стратегия имеет свой strategy_name (для strategy_signals.strategy колонка, VARCHAR(64)),
свой список symbols, свой min_confidence. Одна позиция = один signal → ExecutionAgent.

Преимущества: меньше cron-задач, общий risk-check, легче мониторить.

Стратегии (08.06.2026 — v6/v2 ОТКЛЮЧЕНЫ по приказу Босса):
  - v7a (trailing): SUI/NEAR/SOL/LIT/WLD/TON/DOGE/ADA — 8 пар

Position size: $5 (tier=starter), 1× leverage, max 2 concurrent per pair (через has_open_position).
"""
from __future__ import annotations

import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import pandas as pd

sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

from cryptotrader_strategies.clone_5_v7 import Clone5V7Strategy

DB = dict(
    host="192.168.0.149", port=5432, database="cryptotrader",
    user="cryptotrader", password=os.environ.get("POSTGRES_PASSWORD", ""),
)
TF = "5m"
TIMEFRAME_MIN = 5

# Per-strategy config
STRATEGIES = [
    {
        "name": "clone5_v7_trailing_only",
        "display": "v7a",
        "class": Clone5V7Strategy,
        "symbols": ["SUIUSDT", "NEARUSDT", "SOLUSDT", "LITUSDT",
                    "WLDUSDT", "TONUSDT", "DOGEUSDT", "ADAUSDT"],
        "min_conf": 0.50,
    },
]

# Exchange for live prices (time-sync safe, code review H1)
from cryptotrader_strategies.bybit_safe import bybit_exchange
exchange = bybit_exchange(with_auth=False)


def get_pos_usdt() -> float:
    try:
        state = json.loads(Path('/home/andy/CryptoTrader/compound_state.json').read_text())
        return float(state.get('current_pos_usdt', 5.0))
    except Exception:
        return 5.0


def get_db():
    return psycopg2.connect(**DB)


def get_ohlcv(symbol: str, lookback_bars: int = 300) -> pd.DataFrame:
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
        df = df.iloc[::-1].reset_index(drop=True)
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        df.set_index('timestamp', inplace=True)
        df = df.astype(float)
        return df
    finally:
        conn.close()


def has_open_position(symbol: str, strategy_name: str = None) -> bool:
    """Check open position. If strategy_name given, check only this strategy's positions."""
    conn = get_db()
    try:
        cur = conn.cursor()
        if strategy_name:
            cur.execute("""
                SELECT COUNT(*) FROM positions
                WHERE symbol=%s AND status='open' AND notes LIKE %s
            """, (symbol, f'%{strategy_name}%'))
        else:
            cur.execute("""
                SELECT COUNT(*) FROM positions
                WHERE symbol=%s AND status='open'
            """, (symbol,))
        return cur.fetchone()[0] > 0
    finally:
        conn.close()


def open_signal(strategy_name: str, symbol: str, side: str, sl_pct: float, tp_pct: float,
                score: float, reasoning: str, details: dict) -> int:
    """Create strategy_signals row (ExecutionAgent picks up).

    Сторона записывается в `action` ('BUY' = LONG, 'SELL' = SHORT).
    Размер позиции и side-метаданные — в `exit_plan_json` (читается execution_agent).
    См. code_review/CODE_REVIEW_2026-06-08.md, замечание C1.
    """
    pos_usdt = get_pos_usdt()
    conn = get_db()
    try:
        cur = conn.cursor()
        try:
            ticker = exchange.fetch_ticker(f"{symbol}")
            entry_price = ticker['last']
        except Exception as e:
            print(f"  ✗ {symbol}: ticker fetch failed: {e}", flush=True)
            return -1
        if side == 'LONG':
            sl_price = entry_price * (1 - sl_pct / 100)
            tp_price = entry_price * (1 + tp_pct / 100)
            action = 'BUY'
        else:
            sl_price = entry_price * (1 + sl_pct / 100)
            tp_price = entry_price * (1 - tp_pct / 100)
            action = 'SELL'
        # Write side/position to exit_plan_json; legacy details still in reasoning JSON.
        exit_plan = {
            "side": side,
            "position_usdt": pos_usdt,
            "leverage": 1,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
        }
        reasoning_text = json.dumps({
            "details": details,
            "reasoning": reasoning,
        }, default=str)
        cur.execute("""
            INSERT INTO strategy_signals
              (strategy, symbol, action, confidence, entry_price, stop_loss, take_profit,
               timeframes, reasoning, status, created_at, exchange, exit_plan_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', NOW(), 'bybit', %s)
            RETURNING id
        """, (
            strategy_name, symbol, action, float(score), entry_price, sl_price, tp_price,
            TF, reasoning_text, json.dumps(exit_plan, default=str),
        ))
        sig_id = cur.fetchone()[0]
        conn.commit()
        print(f"  ✓ [{strategy_name}] OPEN {side} {symbol} @ ${entry_price:.4f}  "
              f"SL=${sl_price:.4f} TP=${tp_price:.4f}  score={score:.2f}  pos=${pos_usdt:.2f}",
              flush=True)
        return sig_id
    except Exception as e:
        conn.rollback()
        print(f"  ✗ {strategy_name} {symbol}: INSERT failed: {e}", flush=True)
        return -1
    finally:
        conn.close()


def scan_strategy(cfg: dict) -> int:
    """One pass for one strategy. Returns signal count."""
    strategy = cfg["class"]()
    min_conf = cfg["min_conf"]
    signals = 0
    print(f"\n[{cfg['display']}] {cfg['name']} — {len(cfg['symbols'])} pairs", flush=True)
    for sym in cfg["symbols"]:
        # Check this strategy already has open position on this symbol
        if has_open_position(sym, cfg["name"]):
            print(f"  ⊘ {sym}: already open by {cfg['name']}", flush=True)
            continue
        df = get_ohlcv(sym, lookback_bars=300)
        if df.empty or len(df) < 100:
            print(f"  ⊘ {sym}: no data ({len(df)} bars)", flush=True)
            continue
        # M1: stale-guard — последний бар не старше 3×TF (15 мин для 5m)
        # df.index — это DatetimeIndex, index[-1] — Timestamp (pyright не видит из-за generic Index type).
        last_ts: pd.Timestamp = df.index[-1]  # type: ignore[assignment]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize('UTC')
        age_min = (datetime.now(timezone.utc) - last_ts.to_pydatetime()).total_seconds() / 60
        if age_min > 3 * TIMEFRAME_MIN:
            print(f"  ⊘ {sym}: stale data ({age_min:.0f}m old > {3 * TIMEFRAME_MIN}m), skip", flush=True)
            continue
        # M7: отбрасываем формирующийся бар, если он ещё не закрыт (age < TF)
        if age_min < TIMEFRAME_MIN:
            df = df.iloc[:-1]
            if len(df) < 100:
                print(f"  ⊘ {sym}: only forming bar ({age_min:.1f}m), skip", flush=True)
                continue
        decision = strategy.decide(df, sym)
        sig = decision.get('signal', 'HOLD')
        if sig == 'HOLD':
            continue
        if decision.get('confidence', 0) < min_conf:
            continue
        side = decision.get('side')
        if side is None:
            side = 'LONG' if sig == 'BUY' else 'SHORT'
        sl_pct = decision.get('stop_loss_pct', 0.5)
        tp_pct = decision.get('take_profit_pct', 2.0)
        score = decision.get('confidence', 0.5)
        details = decision.get('details', {})
        details['reasoning'] = decision.get('reasoning', '')
        sig_id = open_signal(
            cfg["name"], sym, side, sl_pct, tp_pct, score,
            decision.get('reasoning', ''), details,
        )
        if sig_id > 0:
            signals += 1
    return signals


def main():
    # L1: убрали первый заголовок (дубль в clone5_multi_cron.py)
    print(f"Strategies: {len(STRATEGIES)} ({', '.join(s['display'] for s in STRATEGIES)})", flush=True)
    print(f"Pos size: ${get_pos_usdt():.2f} (from compound_state.json)", flush=True)
    print("=" * 80, flush=True)
    total = 0
    for cfg in STRATEGIES:
        try:
            n = scan_strategy(cfg)
            total += n
        except Exception as e:
            print(f"  ! [{cfg['display']}] error: {e}", flush=True)
    print(f"\n=== Total signals this scan: {total} ===", flush=True)
    return total


if __name__ == "__main__":
    main()
