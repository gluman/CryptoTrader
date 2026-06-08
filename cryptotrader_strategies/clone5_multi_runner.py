#!/usr/bin/env python3
"""
clone5_multi_runner.py — producer сигналов для боевого Clone5-контура.

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ
═══════════════════════════════════════════════════════════════════════════════

История: code review `code_review/CODE_REVIEW_2026-06-08.md`, замечания
C1 (action='OPEN' не исполняется), C2 (нет исполнителя), M1 (stale-guard),
M7 (forming bar), L1 (дубль заголовка), L7 (os.environ crash).

Сканирует 1 стратегию (v7a) на 8 парах с 5m таймфреймом, на каждом
тика cron `e020bee290d2` (every 60m) пишет сигналы в `strategy_signals`.
Затем исполнитель `execute_cron.py` (cron `ce83cb821455`, every 3m) их
подхватывает.

═══════════════════════════════════════════════════════════════════════════════
ЧТО ДЕЛАЕТ
═══════════════════════════════════════════════════════════════════════════════

  1. Загружает OHLCV 5m (300 баров = 25 часов истории) из БД.
  2. Проверяет stale-guard: если последний бар старше 15 мин (3×TF) →
     skip пару (M1: иначе торгуем на устаревших данных).
  3. Отбрасывает формирующийся бар: если ему < 5 мин → iloc[:-1]
     (M7: иначе индикаторы считаются на неполном баре).
  4. Вызывает `Clone5V7Strategy.decide()` → получает dict с action,
     side, SL, TP, score, position_usdt, leverage.
  5. Если action != 'HOLD' → INSERT в `strategy_signals` с action=
     'BUY' (LONG) или 'SELL' (SHORT) (C1: 'OPEN' не читает исполнитель).
  6. side + position_usdt + leverage + sl_pct + tp_pct кладутся в
     `exit_plan_json` (JSONB), а не в `reasoning` (был баг ранее).

═══════════════════════════════════════════════════════════════════════════════
СТРАТЕГИИ (08.06.2026 14:50 МСК)
═══════════════════════════════════════════════════════════════════════════════

  - v7a (trailing): SUI/NEAR/SOL/LIT/WLD/TON/DOGE/ADA — 8 пар
  - v6 (ICT+Bulkowski+VSA+Spring/UTAD): ОТКЛЮЧЕНА 08.06 по приказу Босса
  - v2 (ICT only): ОТКЛЮЧЕНА 08.06 по приказу Босса

Если потребуется re-enable v6/v2 — добавить dict в STRATEGIES list,
вернуть импорты Clone5V6Strategy / Clone5V2Strategy, согласовать L4
(min_score_to_trade=0.55 vs base=0.50).

═══════════════════════════════════════════════════════════════════════════════
ПОЧЕМУ ИМЕННО ТАК, А НЕ ИНАЧЕ
═══════════════════════════════════════════════════════════════════════════════

  1. **action='BUY'/'SELL' вместо 'OPEN'** (C1):
     ExecutionAgent (`src/agents/execution_agent.py:_execute_pending_buys`
     + `_execute_pending_sells`) читает ТОЛЬКО `action='BUY'` для
     LONG и `action='SELL'` для SHORT. action='OPEN' — был легаси-
     синтаксис, который никто не обрабатывал. side теперь лежит в
     exit_plan_json (а не в JSON-строке внутри reasoning).

  2. **exit_plan_json (JSONB) вместо reasoning (TEXT)** (C1 + L7):
     JSONB позволяет читать структурированные поля без json.loads()
     на каждой итерации. Postgres индексирует JSONB по ключам, плюс
     старый код мог не распарсить JSON из-за экранирования.

  3. **Stale-guard 3×TF** (M1):
     На 5m TF: последний бар не старше 15 мин. Если DataCollector
     опоздал (SUI однажды был stale 97 мин), мы не будем торговать
     на устаревших данных — skip пару.

  4. **Drop forming bar** (M7):
     Если age_min < TF (5 мин для 5m) — последний бар ещё
     формируется, объём/OHLC могут прыгнуть в последние секунды.
     iloc[:-1] отбрасывает его.

  5. **One signal per pair per scan**:
     Для каждой пары в каждой стратегии — максимум 1 сигнал за
     scan. Если `has_open_position(sym, strategy_name)` уже true —
     skip (нужно для per-strategy dedup, M2).

  6. **Position size из compound_state.json**:
     Размер позиции читается из `compound_state.json`
     (по умолчанию $5 для tier=starter). Это значение попадает в
     `exit_plan_json.position_usdt` и читается в ExecutionAgent
     (см. H2 фикс).

  7. **DB connection per query** (get_db() → psycopg2.connect):
     Сканер работает < 10 сек. Не держим долгоживущий connection —
     проще переподключаться, чем ловить idle disconnects.

  8. **POSTGRES_PASSWORD через os.environ.get(..., '')** (L7):
     Если переменная не задана — пустая строка. Connection упадёт
     позже с понятной ошибкой psycopg2, а не с KeyError до
     import'а модуля.

═══════════════════════════════════════════════════════════════════════════════
КОНФИГ
═══════════════════════════════════════════════════════════════════════════════

  /home/andy/CryptoTrader/.env:
    POSTGRES_PASSWORD=***
    BYBIT_API_KEY=***
    BYBIT_API_SECRET=***        (опционально, для live-тикеров)

  /home/andy/CryptoTrader/compound_state.json:
    {"tier": "starter", "current_pos_usdt": 5.0, ...}

═══════════════════════════════════════════════════════════════════════════════
СМ. ТАКЖЕ
═══════════════════════════════════════════════════════════════════════════════

  • code_review/CODE_REVIEW_2026-06-08.md, §0.4 R1 (C1+C2 фиксы)
  • code_review/CODE_REVIEW_2026-06-08.md, §C1 (action='BUY'/'SELL')
  • code_review/CODE_REVIEW_2026-06-08.md, §M1, §M7 (stale-guard + forming bar)
  • cryptotrader_strategies/clone_5_v7.py — реализация v7a стратегии
  • cryptotrader_strategies/execute_cron.py — потребитель сигналов
  • ~/.hermes/cron/jobs.json — cron `e020bee290d2` (every 60m) для clone5_multi_cron.py
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

# L7 fix: os.environ.get + fallback '' вместо KeyError.
# Если POSTGRES_PASSWORD не задан — connection упадёт с понятной ошибкой psycopg2.
DB = dict(
    host="192.168.0.149", port=5432, database="cryptotrader",
    user="cryptotrader", password=os.environ.get("POSTGRES_PASSWORD", ""),
)
TF = "5m"
TIMEFRAME_MIN = 5

# Per-strategy config.
# 08.06.2026: v6/v2 отключены по приказу Босса. Если потребуется re-enable —
# добавить dict ниже + вернуть импорты Clone5V6Strategy/Clone5V2Strategy.
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

# Exchange for live prices (H1: time-sync safe через bybit_safe.py).
# Публичный клиент — ключи не нужны (fetch_ticker, fetch_ohlcv публичные).
from cryptotrader_strategies.bybit_safe import bybit_exchange
exchange = bybit_exchange(with_auth=False)


def get_pos_usdt() -> float:
    """Читает compound_state.json. Возвращает текущий размер позиции в USDT.

    Используется как fallback если `exit_plan.position_usdt` не задан в
    сигнале (см. H2 fix в execution_agent.py).
    """
    try:
        state = json.loads(Path('/home/andy/CryptoTrader/compound_state.json').read_text())
        return float(state.get('current_pos_usdt', 5.0))
    except Exception:
        return 5.0


def get_db():
    """Новое подключение к БД (psycopg2). Сканер короткоживущий, не держим pool."""
    return psycopg2.connect(**DB)


def get_ohlcv(symbol: str, lookback_bars: int = 300) -> pd.DataFrame:
    """Загрузить 5m OHLCV (последние N баров) из БД.

    Returns:
        pd.DataFrame с колонками [open, high, low, close, volume],
        index = DatetimeIndex (UTC, tz-aware).
    """
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
