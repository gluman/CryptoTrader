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

# R13 FIX: единый источник DSN — db_safe.db_dsn() (убран дубль хардкода .149).
# Если POSTGRES_PASSWORD не задан — connection упадёт с понятной ошибкой psycopg2.
from cryptotrader_strategies.db_safe import db_dsn
DB = db_dsn()
# R16 (17.06): откат 15m → 5m. Сравнение показало PF 1.89 (5m) vs 1.02 (15m).
TF = "5m"
TIMEFRAME_MIN = 5
# R3 FIX: единый лимит одновременных позиций (было 1/2/3/8 в разных местах).
# Теперь один источник истины, enforced в продьюсере.
MAX_CONCURRENT = 2

# Per-strategy config.
# 08.06.2026: v6/v2 отключены по приказу Босса. Если потребуется re-enable —
# добавить dict ниже + вернуть импорты Clone5V6Strategy/Clone5V2Strategy.
STRATEGIES = [
    {
        "name": "clone5_v7_trailing_only",
        "display": "v7a",
        "class": Clone5V7Strategy,
        # [Fix 2026-06-17] TONUSDT убран — Bybit closed контракт (status=Closed,
        # delivery 12.06.2026). OHLCV stale навсегда, позиция неисполнима.
        # [Fix 17.07.2026 Босс] WLDUSDT, XRPUSDT убраны — WR 16.7% и 0% за 06-12.07,
        # тянут equity вниз. Регрессия 36 trades за неделю: WLD −$0.318, XRP −$0.215.
        # [Fix 19.07.2026 Босс] добавлены AKE/SOXL/ZEC/ENA/APT/TAO/AVAX/AAVE —
        # profit factor > 1.5 на 30d бэктесте, BB%>1.5, left-skew (наш setup).
        # [Fix 19.07.2026 Босс] добавлены BCH/DYDX/AXS/MAGIC/KSM —
        # топ-5 расширенного скрининга 60+ пар (PF > 2.5).
        "symbols": ["SUIUSDT", "NEARUSDT", "SOLUSDT", "LITUSDT",
                    "DOGEUSDT", "ADAUSDT",
                    "AKEUSDT", "SOXLUSDT", "ZECUSDT", "ENAUSDT",
                    "APTUSDT", "TAOUSDT", "AVAXUSDT", "AAVEUSDT",
                    "BCHUSDT", "DYDXUSDT", "AXSUSDT", "MAGICUSDT", "KSMUSDT"],
        # [Fix 22.07.2026 Босс] min_conf 0.50→0.60 — синхронизировано с clone_5_v7.py params.
        # Оптимальная комбинация из анализа 30д × 19 пар (см. /tmp/threshold_analysis.py).
        "min_conf": 0.60,
    },
]

# ═══════════════════════════════════════════════════════════════════════════════
# R14 FIX (14.06.2026): убран live ticker-fetch. entry_price = close последнего
# бара из БД. Причина: scan interval 180m→5m — ticker-fetch на каждом сигнале
# съел бы quota. entry_price в сигнале справочный (executor открывает по market
# order, SL/TP пересчитываются от fill price — R10 fix). Экономия: 0 API req на
# скан вместо 1 req/сигнал. Зато сканер работает чисто по DB-данным.
# ═══════════════════════════════════════════════════════════════════════════════


def get_pos_usdt() -> float:
    """Читает compound_state.json. Возвращает текущий размер позиции в USDT.

    Используется как fallback если `exit_plan.position_usdt` не задан в
    сигнале (см. H2 fix в execution_agent.py).

    R22 (08.07.2026): size пересмотрен с $7 → $15. Причина: при $7 notional
    fee round-trip ~0.21% ВСЕГДА съедает gross-PnL, даже при достижении SL.
    $15 notional уменьшает fee% до 0.10%, оставляя реальный edge.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / 'compound_state.json',  # CryptoTrader_main/compound_state.json
        Path('/home/andy/CryptoTrader_main/compound_state.json'),
    ]
    for p in candidates:
        try:
            state = json.loads(p.read_text())
            return float(state.get('current_pos_usdt', 15.0))
        except Exception:
            continue
    return 15.0


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
                score: float, reasoning: str, details: dict,
                entry_price: float = 0.0) -> int:
    """Create strategy_signals row (ExecutionAgent picks up).

    Сторона записывается в `action` ('BUY' = LONG, 'SELL' = SHORT).
    Размер позиции и side-метаданные — в `exit_plan_json` (читается execution_agent).
    См. code_review/CODE_REVIEW_2026-06-08.md, замечание C1.

    R14 FIX (14.06.2026): entry_price передаётся вызывающим кодом (= close
    последнего бара из БД). Никакого API-вызова — 0 req/quota. Раньше тут
    был exchange.fetch_ticker(unified) на каждый сигнал. entry_price в сигнале
    справочный — executor открывает по market order, SL/TP пересчитываются
    от fill price (R10 fix).
    """
    pos_usdt = get_pos_usdt()
    conn = get_db()
    try:
        cur = conn.cursor()
        if side == 'LONG':
            sl_price = entry_price * (1 - sl_pct / 100)
            tp_price = entry_price * (1 + tp_pct / 100)
            action = 'BUY'
        else:
            sl_price = entry_price * (1 + sl_pct / 100)
            tp_price = entry_price * (1 - tp_pct / 100)
            action = 'SELL'
        # Write side/position to exit_plan_json; legacy details still in reasoning JSON.
        # R10 FIX: SL/TP передаются в процентах (sl_pct/tp_pct) — абсолютные уровни
        # вычисляются на стороне executor от фактической цены fill, а не от цены скана.
        # entry_price в сигнале — только референс для логов.
        # R17: regime info в exit_plan для логирования (что trailing был ON/OFF при открытии)
        exit_plan = {
            "side": side,
            "position_usdt": pos_usdt,
            "leverage": 1,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "sl_ref": sl_price,   # R10: справочно, для логов
            "tp_ref": tp_price,   # R10: справочно, для логов
            "vol_regime": details.get("vol_regime", "unknown"),
            "vol_bb_width": details.get("vol_bb_width", 0.0),
            "trailing_on": details.get("vol_regime") == "high",  # R17: trailing on iff regime=high
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


def count_open_positions() -> int:
    """R3: Count total open positions across all strategies."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM positions WHERE status='open'")
        return cur.fetchone()[0]
    finally:
        conn.close()


def compute_vol_regime(symbols: list, threshold: float = 0.6) -> tuple[str, float]:
    """R17 (18.06.2026): volatility regime detector.
    Считает BB width (Bollinger Band width) среднее по 8 парам на последних
    20 барах × 5m. Если > threshold → vol_high (trailing on), else vol_low.

    P0-FIX (15.07.2026): threshold 1.0% → 0.6%.
    ЗАЧЕМ: Optimal 1.0% был найден на бэктесте 18.06, но за последние 5 дней
    BB width держится 0.5-1.2% (chop-рынок, Fear=22). При threshold=1.0%
    regime=low 90% времени → trailing=OFF → 0 сигналов.
    При threshold=0.6% regime=high включается чаще, бот реагирует на
    chop-движения (1-2% range). Grid search не делал — это conservative
    compromise, ожидаю больше сигналов без ухудшения WR.

    Возвращает (regime, bb_width).
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        bb_widths = []
        for sym in symbols:
            cur.execute("""
                SELECT close FROM ohlcv_raw
                WHERE exchange='bybit' AND timeframe=%s AND symbol=%s
                ORDER BY timestamp DESC LIMIT 20
            """, (TF, sym))
            rows = [r[0] for r in cur.fetchall()]
            if len(rows) < 20:
                continue
            closes = pd.Series(rows[::-1])  # reverse to chronological
            ma = closes.rolling(20).mean().iloc[-1]
            sd = closes.rolling(20).std().iloc[-1]
            if ma and ma > 0:
                bb_w = ((ma + 2 * sd) - (ma - 2 * sd)) / ma * 100
                bb_widths.append(bb_w)
        if not bb_widths:
            return ("unknown", 0.0)
        avg_bb = float(pd.Series(bb_widths).mean())
        regime = "high" if avg_bb > threshold else "low"
        return (regime, avg_bb)
    except Exception as e:
        print(f"  ⚠ compute_vol_regime failed: {e}", flush=True)
        return ("unknown", 0.0)
    finally:
        conn.close()


def compute_higher_tf_trends(symbols: list) -> dict:
    """R18 (18.06.2026): Multi-TF trend detection (E2 gap-filter).

    Для каждой пары загружает 1h OHLCV (50 баров) и определяет тренд:
    - "up": SMA20 > SMA50 (или price > SMA20 > SMA50)
    - "down": SMA20 < SMA50
    - "flat": SMA20 ≈ SMA50 (within 0.3%)

    Returns:
        {"SUIUSDT": "up", "DOGEUSDT": "down", ...}
    """
    conn = get_db()
    trends = {}
    try:
        cur = conn.cursor()
        for sym in symbols:
            cur.execute("""
                SELECT close FROM ohlcv_raw
                WHERE exchange='bybit' AND timeframe='1h' AND symbol=%s
                ORDER BY timestamp DESC LIMIT 50
            """, (sym,))
            rows = [float(r[0]) for r in cur.fetchall()]
            if len(rows) < 50:
                trends[sym] = "unknown"
                continue
            closes = pd.Series(rows[::-1])  # chronological
            sma20 = closes.iloc[-20:].mean()
            sma50 = closes.iloc[-50:].mean()
            if sma50 <= 0:
                trends[sym] = "unknown"
                continue
            diff_pct = (sma20 - sma50) / sma50 * 100
            if diff_pct > 0.3:
                trends[sym] = "up"
            elif diff_pct < -0.3:
                trends[sym] = "down"
            else:
                trends[sym] = "flat"
        return trends
    except Exception as e:
        print(f"  ⚠ compute_higher_tf_trends failed: {e}", flush=True)
        return {sym: "unknown" for sym in symbols}
    finally:
        conn.close()


def compute_market_conditions(symbols: list, lookback_1h: int = 100) -> dict:
    """R18 (18.06.2026): Market condition detection (B4 gap-filter).

    Для каждой пары смотрит 1h close за последние ~4 дня (100h).
    - "bull": текущая price > SMA50h на >3%
    - "bear": текущая price < SMA50h на >3%
    - "neutral": within 3%

    Springs надёжнее в bear (Bulkowski DB bust=10.2%), UTADs в bull.

    Returns:
        {"SUIUSDT": "bear", "DOGEUSDT": "bull", ...}
    """
    conn = get_db()
    conditions = {}
    try:
        cur = conn.cursor()
        for sym in symbols:
            cur.execute("""
                SELECT close FROM ohlcv_raw
                WHERE exchange='bybit' AND timeframe='1h' AND symbol=%s
                ORDER BY timestamp DESC LIMIT %s
            """, (sym, lookback_1h))
            rows = [float(r[0]) for r in cur.fetchall()]
            if len(rows) < 50:
                conditions[sym] = "unknown"
                continue
            closes = pd.Series(rows[::-1])
            current = closes.iloc[-1]
            sma50 = closes.iloc[-50:].mean()
            if sma50 <= 0:
                conditions[sym] = "unknown"
                continue
            dev = (current - sma50) / sma50 * 100
            if dev > 3.0:
                conditions[sym] = "bull"
            elif dev < -3.0:
                conditions[sym] = "bear"
            else:
                conditions[sym] = "neutral"
        return conditions
    except Exception as e:
        print(f"  ⚠ compute_market_conditions failed: {e}", flush=True)
        return {sym: "unknown" for sym in symbols}
    finally:
        conn.close()


def _scan_llm_primary(cfg: dict, strategy, regime: str, bb_w: float) -> int:
    """═══════════════════════════════════════════════════════════════════════════
    R20 (19.06.2026): LLM-Primary scan (Mode D) — вызывается при vol_regime=high.
    ════════════════════════════════════════════════════════════════════════════

    ЗАЧЕМ:
      Босс приказал: LLM должна "следовать нашей стратегии" — быть primary
      decision-maker, а не пассивным post-filter. В high-vol regime M3 решает
      BUY/SELL/HOLD по Composite MM правилам для ВСЕХ пар (не только v7a
      кандидатов). v7a детектор → поставщик индикаторов.

    ЧТО ДЕЛАЕТ:
      1. Для каждой пары: загружает OHLCV, вызывает v7a.decide() для индикаторов
         (v7a signal/confidence идут в LLM как reference, не как фильтр).
      2. Параллельно (ThreadPool, 8 workers) вызывает llm_primary_decision() для
         всех годных пар. Wall time ~24с (бенчмарк 19.06), укладывается в 5m окно.
      3. Для каждого LLM-confirmed (BUY/SELL, conf>=0.55): open_signal().
      4. Логирует ВСЕ решения LLM (включая HOLD/REJECTED) — для отчёта Боссу.

    ПОЧЕМУ ПАРАЛЛЕЛЬНО:
      Последовательно 8 пар × ~8с = 64с — приемлемо, но параллельно = ~24с.
      MiniMax принимает конкурентные запросы (verified 19.06). ThreadPool безопасен
      т.к. llm_primary_decision stateless (только HTTP + JSON parse).

    COST:
      ~при vol_regime=high (~60% времени): 8 пар × 288 тиков × 0.6 ≈ 1382 calls/day.
      Босс одобрил "только при vol_regime=high" (≈1000 calls/day).

    GRACEFUL FALLBACK:
      Если LLM недоступна для пары → llm_primary_decision вернёт confirmed=False
      (safe hold). Трейда нет, но сканер не падает.

    СМ. ТАКЖЕ:
      - mm_llm_gate.py:llm_primary_decision() — ядро Mode D
      - skill: composite-mm-llm-pipeline (раздел R20)
    ════════════════════════════════════════════════════════════════════════════

    Returns:
        Количество открытых сигналов (LLM-confirmed).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from cryptotrader_strategies.mm_llm_gate import llm_primary_decision

    min_conf = cfg["min_conf"]
    signals = 0

    # --- Phase 1: собрать decisions для всех пар (v7a = индикаторы) ---
    pairs_data = []  # [(sym, df, decision), ...]
    for sym in cfg["symbols"]:
        # R3: глобальный лимит позиций
        open_total = count_open_positions()
        if open_total + signals >= MAX_CONCURRENT:
            print(f"  ⊘ global cap {MAX_CONCURRENT} reached, stop LLM scan", flush=True)
            break
        if has_open_position(sym, cfg["name"]):
            print(f"  ⊘ {sym}: already open by {cfg['name']}", flush=True)
            continue
        df = get_ohlcv(sym, lookback_bars=300)
        if df.empty or len(df) < 100:
            print(f"  ⊘ {sym}: no data ({len(df)} bars)", flush=True)
            continue
        last_ts: pd.Timestamp = df.index[-1]  # type: ignore[assignment]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize('UTC')
        age_min = (datetime.now(timezone.utc) - last_ts.to_pydatetime()).total_seconds() / 60
        if age_min > 3 * TIMEFRAME_MIN:
            print(f"  ⊘ {sym}: stale data ({age_min:.0f}m), skip", flush=True)
            continue
        if age_min < TIMEFRAME_MIN:
            df = df.iloc[:-1]
            if len(df) < 100:
                print(f"  ⊘ {sym}: only forming bar, skip", flush=True)
                continue
        # v7a decide — поставляет индикаторы. Signal/confidence = reference для LLM.
        decision = strategy.decide(df, sym)
        # Дополняем details ценой close (для LLM context)
        decision.setdefault("details", {})["close"] = float(df.iloc[-1]['close'])
        pairs_data.append((sym, df, decision))

    if not pairs_data:
        return 0

    print(f"  🧠 Mode D (LLM primary): querying M3 for {len(pairs_data)} pairs "
          f"(parallel)...", flush=True)

    # --- Phase 2: параллельный LLM call для всех пар ---
    llm_start = datetime.now()
    results: dict = {}  # sym -> llm_result
    errors: dict = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(llm_primary_decision, sym, TF, dec, df): sym
            for sym, df, dec in pairs_data
        }
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                results[sym] = fut.result()
            except Exception as e:
                errors[sym] = str(e)
    llm_elapsed = (datetime.now() - llm_start).total_seconds()
    # ═══ FIX 23.07.2026 (Босс): корректный подсчёт успехов LLM ═══
    # llm_primary_decision НЕ бросает исключение на HTTP 429 / parse-fail — она
    # возвращает dict с полем error='llm_call_failed' (Mode D safe-hold). Раньше
    # такие попадали в results и считались "ok" → лог "19 ok, 0 errors" маскировал
    # тотальный провал (напр. M3 429 token-limit). Теперь ok = только dict без error.
    ok = 0
    failed_reasons: list = []
    for r in results.values():
        if isinstance(r, dict) and not r.get("error"):
            ok += 1
        else:
            failed_reasons.append(r.get("error", "bad_result") if isinstance(r, dict) else "none_result")
    line = (f"  ⏱ LLM batch done in {llm_elapsed:.1f}s "
            f"({ok} ok, {len(failed_reasons)} failed, {len(errors)} errors)")
    if failed_reasons:
        from collections import Counter
        reason, cnt = Counter(failed_reasons).most_common(1)[0]
        line += f" — {cnt}× {reason}"
    print(line, flush=True)

    # --- Phase 3: логировать ВСЕ решения, открыть confirmed ---
    df_by_sym = {sym: df for sym, df, _ in pairs_data}
    dec_by_sym = {sym: dec for sym, _, dec in pairs_data}
    for sym in cfg["symbols"]:
        if sym in errors:
            print(f"  ⚠ {sym}: LLM error: {errors[sym]}", flush=True)
            continue
        if sym not in results:
            continue
        llm_res = results[sym]
        decision = dec_by_sym[sym]
        v7a_sig = decision.get('signal', 'HOLD')
        llm_sig = llm_res.get('llm_signal', 'HOLD')
        llm_conf = llm_res.get('llm_confidence', 0)
        co = llm_res.get('co_action', 'unknown')
        agree = llm_res.get('agree', False)
        err = llm_res.get('error')

        # Логируем каждое решение LLM (для отчёта Боссу — даже HOLD)
        if llm_sig in ('BUY', 'SELL'):
            tag = "✓ EXEC" if llm_res['confirmed'] else "✗ SKIP"
            agree_tag = "agree" if agree else "OVERRIDE"
            print(f"  {tag} {sym}: LLM {llm_sig} conf={llm_conf:.2f} co={co} "
                  f"v7a={v7a_sig}({agree_tag})", flush=True)
            print(f"      reasoning: {llm_res.get('llm_reasoning', '')}", flush=True)
        else:
            # HOLD — логируем кратко (для частоты в отчёте)
            print(f"  ⊘ {sym}: LLM HOLD conf={llm_conf:.2f} co={co} "
                  f"v7a={v7a_sig}" + (f" [{err}]" if err else ""), flush=True)

        # R3: проверка cap перед открытием
        open_total = count_open_positions()
        if open_total + signals >= MAX_CONCURRENT:
            print(f"  ⊘ global cap {MAX_CONCURRENT} reached, stop opening", flush=True)
            break

        if not llm_res['confirmed']:
            continue

        # Открыть LLM-confirmed сигнал (SL/TP из LLM решения)
        # ═══ FIX 2026-07-08 (Босс R22): SL/TP расширены для fee-survival ═══
        #   • Было: sl_pct=0.5, tp_pct=2.0  → fee round-trip ~0.21% on $7 notional
        #   • Стало: sl_pct=1.2, tp_pct=3.5  → R:R 1:2.9, gross>fee даже при noise SL
        #   • LLM-override (llm_sl_pct/llm_tp_pct) уважаются; default поднят если LLM не дала
        side = llm_res.get('llm_side', 'LONG')
        sl_pct = llm_res.get('llm_sl_pct', decision.get('stop_loss_pct', 1.2))
        tp_pct = llm_res.get('llm_tp_pct', decision.get('take_profit_pct', 3.5))
        score = llm_conf  # score = LLM confidence (LLM primary)
        details = decision.get('details', {})
        details['reasoning'] = decision.get('reasoning', '')
        # R20: LLM primary audit trail
        details['llm_confirmed'] = llm_res.get('confirmed', True)
        details['llm_confidence'] = llm_conf
        details['llm_co_action'] = co
        details['llm_reasoning'] = llm_res.get('llm_reasoning', '')
        details['llm_mode'] = 'D_primary'
        details['llm_v7a_signal'] = v7a_sig
        details['llm_agree'] = agree
        details['llm_signal'] = llm_sig
        # R14: entry_price = close последнего закрытого бара
        df = df_by_sym[sym]
        entry_price = float(df.iloc[-1]['close'])
        sig_id = open_signal(
            cfg["name"], sym, side, sl_pct, tp_pct, score,
            decision.get('reasoning', ''), details,
            entry_price=entry_price,
        )
        if sig_id > 0:
            signals += 1

    return signals


def scan_strategy(cfg: dict) -> int:
    """One pass for one strategy. Returns signal count."""
    strategy = cfg["class"]()
    min_conf = cfg["min_conf"]
    signals = 0
    print(f"\n[{cfg['display']}] {cfg['name']} — {len(cfg['symbols'])} pairs", flush=True)
    # R17: compute vol regime once per scan (BB width market_avg).
    # Apply to strategy instance — sets trailing_enabled based on regime.
    regime, bb_w = compute_vol_regime(cfg["symbols"], threshold=1.0)
    strategy.set_vol_regime(regime, bb_w)
    trailing_actual = strategy.params.trailing_enabled
    print(f"  vol_regime={regime}  bb_width={bb_w:.3f}%  trailing={'ON' if trailing_actual else 'OFF'}", flush=True)

    # ═══ R18: Multi-TF trend + market condition ═══
    trends = compute_higher_tf_trends(cfg["symbols"])
    conditions = compute_market_conditions(cfg["symbols"])
    if hasattr(strategy, "set_higher_tf_trends"):
        strategy.set_higher_tf_trends(trends)
    if hasattr(strategy, "set_market_conditions"):
        strategy.set_market_conditions(conditions)
    print(f"  trends: {trends}", flush=True)
    print(f"  market: {conditions}", flush=True)

    # ═══ R20 (19.06.2026): LLM-Primary dispatch по vol_regime ═══
    # Босс: "LLM должна следовать нашей стратегии" — LLM = primary decision-maker.
    # Но только при vol_regime=high (≈1000 calls/day; в low-vol LLM всё равно HOLD).
    # - vol_regime=high → Mode D (_scan_llm_primary): LLM решает по всем парам.
    # - vol_regime=low  → Mode C (текущая логика ниже): v7a детектор + LLM gate.
    if regime == "high":
        print(f"  🧠 R20 Mode D: LLM primary (vol_regime=high)", flush=True)
        return _scan_llm_primary(cfg, strategy, regime, bb_w)

    # ═══ R18: LLM HYBRID gate (Mode C) — fallback для low-vol ═══
    # M3 LLM загружается lazily — импорт только если будет non-HOLD candidate.
    llm_gate = None  # импортируется при первом non-HOLD (см. ниже)

    for sym in cfg["symbols"]:
        # R3 FIX: глобальный лимит одновременных позиций
        open_total = count_open_positions()
        if open_total + signals >= MAX_CONCURRENT:
            print(f"  ⊘ global cap {MAX_CONCURRENT} reached ({open_total} open + {signals} new), stop scan", flush=True)
            break
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

        # ═══ R18 (18.06.2026): LLM HYBRID Gate (Mode C) ═══
        # v7a нашла кандидата (spring/UTAD). Прежде чем открыть позицию —
        # запрашиваем M3 LLM через Composite MM master-prompt.
        # ~30 calls/day × 2188 tokens = 65K tokens/day.
        # Graceful fallback: если LLM недоступен — пропускаем signal (v7a trusted).
        llm_result = None
        try:
            if llm_gate is None:
                from cryptotrader_strategies.mm_llm_gate import llm_confirm_signal
                llm_gate = llm_confirm_signal
            llm_result = llm_gate(sym, TF, decision)
            if not llm_result["confirmed"]:
                print(f"  ✗ {sym}: LLM REJECTED v7a {sig} "
                      f"(llm={llm_result['llm_signal']}/{llm_result['llm_confidence']:.2f} "
                      f"co={llm_result['co_action']})",
                      flush=True)
                print(f"      reasoning: {llm_result['llm_reasoning']}",
                      flush=True)
                continue
            if llm_result.get("error") is None:
                print(f"  ✓ {sym}: LLM CONFIRMED {sig} "
                      f"(llm_conf={llm_result['llm_confidence']:.2f} "
                      f"co={llm_result['co_action']} agree={llm_result['agree']})",
                      flush=True)
                print(f"      reasoning: {llm_result['llm_reasoning']}",
                      flush=True)
        except Exception as e:
            print(f"  ⚠ {sym}: LLM gate error (graceful pass-through): {e}", flush=True)

        side = decision.get('side')
        if side is None:
            side = 'LONG' if sig == 'BUY' else 'SHORT'
        # R22 (08.07.2026): SL/TP defaults подняты для fee-survival на $7 notional
        sl_pct = decision.get('stop_loss_pct', 1.2)
        tp_pct = decision.get('take_profit_pct', 3.5)
        score = decision.get('confidence', 0.5)
        details = decision.get('details', {})
        details['reasoning'] = decision.get('reasoning', '')
        # R18: LLM gate result — для мониторинга и audit trail
        if llm_result is not None:
            details['llm_confirmed'] = llm_result.get('confirmed', True)
            details['llm_confidence'] = llm_result.get('llm_confidence', 0)
            details['llm_co_action'] = llm_result.get('co_action', 'unknown')
            details['llm_reasoning'] = llm_result.get('llm_reasoning', '')
        # R14: entry_price = close последнего закрытого бара из БД (без API-вызова)
        entry_price = float(df.iloc[-1]['close'])
        sig_id = open_signal(
            cfg["name"], sym, side, sl_pct, tp_pct, score,
            decision.get('reasoning', ''), details,
            entry_price=entry_price,
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
