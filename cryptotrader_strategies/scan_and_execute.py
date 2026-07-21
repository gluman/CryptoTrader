#!/usr/bin/env python3
"""
scan_and_execute.py — единый scan→execute цикл для v7a стратегии.

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВЕТ
═══════════════════════════════════════════════════════════════════════════════

История: объединение cron-задач e020bee290d2 (scanner) + ce83cb821455
(executor) в один процесс по приказу Босса (14.06.2026).

Проблема двух cron:
  • Scanner пишет signals в БД, executor читает их через 0-5 мин.
    На 5m TF задержка исполнения = до 1 бара = упущенный entry.
  • Два cron = две точки отказа (один упал — цепь разорвана).
  • Двойной import time, двойной DB init, двойной ccxt init.

═══════════════════════════════════════════════════════════════════════════════
ЧТО ДЕЛАЕТ
═══════════════════════════════════════════════════════════════════════════════

  ФАЗА 1 — SCAN (0 API calls, pure DB):
    1. Загружает OHLCV 5m (300 баров) из PostgreSQL для 8 пар.
    2. Stale-guard: если последний бар старше 15 мин (3×TF) → skip.
    3. Drop forming bar: если последний бар младше 5 мин → iloc[:-1].
    4. v7a.decide() → {signal, confidence, side, SL%, TP%}.
    5. Если signal != HOLD и confidence ≥ 0.50 → INSERT strategy_signals.

  ФАЗА 2 — EXECUTE (0-4 API calls, только если есть работа):
    6. ExecutionAgent.run_once() подхватывает ТОЛЬКО ЧТО записанные
       signals + любые pending из предыдущих тиков.
    7. Открывает ордера, ставит SL/TP, ведёт trailing.
    8. Orphan-sync (fetch_positions) — только если в DB есть open positions.

  ФАЗА 3 — REPORT (0 API calls):
    9. JSON summary в stdout для Hermes cron parser.

═══════════════════════════════════════════════════════════════════════════════
ПОЧЕМУ ИМЕННО ТАК, А НЕ ИНАЧЕ
═══════════════════════════════════════════════════════════════════════════════

  1. **Единый процесс = нулевая задержка scan→execute**:
     Раньше: scanner tick → INSERT → ждём executor tick (0-5 мин).
     Теперь: scanner → INSERT → execute в том же процессе (< 1 сек).
     На 5m TF это критично: signal живёт 1-2 бара, задержка = потеря.

  2. **API calls по-прежнему минимальны**:
     SCAN = 0 calls (DB-only, R14 fix сохранён).
     EXECUTE = 0 если нет pending/open. 2-4 если есть signal.
     Orphan-sync fetch_positions ТОЛЬКО если DB имеет open positions.
     Weekly quota: 42-70 req при 2-3 signals/день → <10% лимита.

  3. **Circuit Breaker на 1310 сохранён**:
     Если Bybit вернул 1310 → CB открывается на 30 мин.
     Следующие тики skip-аются без API вызовов.

  4. **no_agent=True (0 токенов LLM)**:
     v7a — детерминированная стратегия (rule-based hunt detection).
     LLM не нужна для принятия торговых решений.
     Script = весь интеллект, stdout = JSON отчёт.

  5. **兼容ность с существующей БД и API**:
     Пишет в ту же strategy_signals, тот же ExecutionAgent.
     Monitor cron (7606e34486dc) продолжает работать без изменений.

═══════════════════════════════════════════════════════════════════════════════
ПРОБЛЕМЫ И КОНТРОЛЬ
═══════════════════════════════════════════════════════════════════════════════

  • Если Bybit 1310 активен — Phase 2 пропускается, Phase 1 работает
    (пишет signals в DB, они исполнятся после reset квоты).
  • Если БД недоступна — весь тик падает с status=error, cron
    помечает как FAILED, следующий тик поднимает новый процесс.
  • MAX_CONCURRENT=2: scanner не создаст сигнал если уже 2 open.
  • compound_state.json: размер позиции читается в scanner и executor.

═══════════════════════════════════════════════════════════════════════════════
СМ. ТАКЖЕ
═══════════════════════════════════════════════════════════════════════════════

  • cryptotrader_strategies/clone5_multi_runner.py — Phase 1 (scan)
  • cryptotrader_strategies/execute_cron.py — Phase 2 (execute)
  • cryptotrader_strategies/clone_5_v7.py — v7a strategy logic
  • src/agents/execution_agent.py — ExecutionAgent.run_once()
  • skill: trading/bybit-trading-orchestration — Pattern 10 (cache-first)
  • skill: cron-no-agent-script-pattern — P2b verify-the-tick rule
"""
from __future__ import annotations

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup (same as clone5_multi_runner + execute_cron) ──
sys.path.insert(0, '/home/andy/CryptoTrader')
sys.path.insert(0, '/home/andy/.hermes/scripts')
from time_utils import now_msk_str, msk_iso_now  # noqa: E402
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

# Логи в stderr (stdout — для JSON-результата планировщика Hermes)
logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO'),
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stderr,
)
log = logging.getLogger('scan_and_execute')


# ═══════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER для Bybit 1310 (сохранён из execute_cron.py без изменений)
# ═══════════════════════════════════════════════════════════════════════════════
CB_STATE_PATH = Path('/tmp/.ct_cb_1310.json')
CB_WINDOW_SECONDS = 300
CB_THRESHOLD_ERRORS = 3
CB_COOLDOWN_SECONDS = 1800


def _cb_load():
    try:
        if CB_STATE_PATH.exists():
            return json.loads(CB_STATE_PATH.read_text())
    except Exception:
        pass
    return {"window": [], "open_until": 0}


def _cb_save(state):
    try:
        CB_STATE_PATH.write_text(json.dumps(state))
    except Exception:
        pass


def _cb_should_skip(state, err_msg: str) -> bool:
    now = time.time()
    msg = (err_msg or '').lower()
    is_1310 = ('1310' in msg) or ('rate limit' in msg) or ('too many visits' in msg)
    if state.get('open_until', 0) > now:
        return True
    if is_1310:
        window = [t for t in state.get('window', []) if now - t < CB_WINDOW_SECONDS]
        window.append(now)
        state['window'] = window
        if len(window) >= CB_THRESHOLD_ERRORS:
            state['open_until'] = now + CB_COOLDOWN_SECONDS
            log.warning(
                f"CircuitBreaker OPEN: {len(window)} 1310-errors in "
                f"{CB_WINDOW_SECONDS}s. Cooldown {CB_COOLDOWN_SECONDS}s."
            )
        _cb_save(state)
    return False


def _cb_reset():
    if CB_STATE_PATH.exists():
        try:
            CB_STATE_PATH.unlink()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: SCAN — v7a strategy by 8 pairs, DB-only (0 API calls)
# ═══════════════════════════════════════════════════════════════════════════════

def phase1_scan() -> dict:
    """Scan v7a strategy on 8 pairs. Write signals to DB.

    Returns:
        {"signals": int, "pairs_scanned": int, "pairs_skipped": int, "error": str?}
    """
    from cryptotrader_strategies.clone5_multi_runner import main as run_scan

    print(f"── Phase 1: SCAN v7a ({now_msk_str()}) ──", flush=True)
    try:
        signals = run_scan()
        result = {"signals": signals, "status": "ok"}
        print(f"  Phase 1 done: {signals} signal(s) generated", flush=True)
        return result
    except Exception as e:
        log.exception(f"Phase 1 (scan) failed: {e}")
        return {"signals": 0, "status": "error", "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: EXECUTE — pending signals → Bybit orders + SL/TP + trailing
# ═══════════════════════════════════════════════════════════════════════════════

def phase2_execute(cb_state: dict) -> dict:
    """Execute pending signals + manage open positions.

    Returns:
        {"buys": int, "sells": int, "sl_tp": int, "cleaned": int,
         "trailing": int, "errors": int, "status": str}
    """
    # Circuit breaker check
    if cb_state.get('open_until', 0) > time.time():
        remaining = int(cb_state['open_until'] - time.time())
        log.info(f"CB open, skip Phase 2 (retry in {remaining}s)")
        return {"status": "skipped", "reason": "circuit_breaker_open",
                "retry_in_seconds": remaining,
                "buys": 0, "sells": 0, "sl_tp": 0, "cleaned": 0,
                "trailing": 0, "errors": 0}

    print(f"── Phase 2: EXECUTE ({now_msk_str()}) ──", flush=True)
    try:
        from src.core.config import Config
        from src.core.database import DatabaseManager
        from src.core.logger import setup_logger
        from src.agents import ExecutionAgent

        config = Config.load()
        ct_logger = setup_logger('cryptotrader', level='INFO',
                                 log_file=config.logging.get('file'))
        db = DatabaseManager(config.postgresql, ct_logger)
        executor = ExecutionAgent(config, ct_logger, db)

        result = executor.run_once(market_type='linear')

        summary = {
            "status": "ok",
            "buys": result.get('buys_executed', 0),
            "sells": result.get('sells_executed', 0),
            "sl_tp": result.get('sl_tp_triggered', 0),
            "cleaned": result.get('signals_cleaned', 0),
            "trailing": result.get('trailing_updated', 0),
            "errors": result.get('errors', 0),
        }
        print(f"  Phase 2 done: buys={summary['buys']} sells={summary['sells']} "
              f"sl_tp={summary['sl_tp']} trailing={summary['trailing']} "
              f"errors={summary['errors']}", flush=True)
        # Success → reset CB
        _cb_reset()
        return summary
    except Exception as e:
        if _cb_should_skip(cb_state, str(e)):
            remaining = int(cb_state.get('open_until', 0) - time.time())
            log.warning(f"1310 detected in Phase 2 — CB now open for {remaining}s")
            return {"status": "skipped", "reason": "circuit_breaker_1310",
                    "retry_in_seconds": remaining,
                    "buys": 0, "sells": 0, "sl_tp": 0, "cleaned": 0,
                    "trailing": 0, "errors": 0}
        log.exception(f"Phase 2 (execute) failed: {e}")
        return {"status": "error", "error": str(e),
                "buys": 0, "sells": 0, "sl_tp": 0, "cleaned": 0,
                "trailing": 0, "errors": 1}


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — единый tick: scan → execute → report
# ═══════════════════════════════════════════════════════════════════════════════

def phase0_collect() -> dict:
    """Собрать свежие OHLCV 5m для 8 пар через BybitAPI (точечно, 8 req).

    ════════════════════════════════════════════════════════════════════════════
    ЗАЧЕМ:
    DataCollectorAgent.run_once() запускался ТОЛЬКО на startup FastAPI.
    Между рестартами данные старели → Phase 1 skip'ал пары по stale-guard.
    Phase 0 собирает 5m бары каждый тик, до scan.

    ПОЧЕМУ ТОЧЕЧНО (а НЕ через run_once):
    • run_once() собирает 5 TF × 10 пар = 50 kline req + RSS news (~8 сек).
    • На 5m cron (288 тиков/день): 50 × 288 = 14 400 req/день = 100 800/week.
      При лимите 1000/week — 100x перебор.
    • Точечный сбор: 1 TF (5m) × 8 пар = 8 req/тик × 288 = 2 304/день
      = 16 128/week. Всё ещё много...

    ── КВОТА Bybit linear: 1000 req/week для tier1. ──
    Phase 0 НЕЛЬЗЯ запускать каждые 5 мин — quota выгорит за 1 час.
    РЕШЕНИЕ: Phase 0 вызывается из main(), но main() запускается cron'ом
    каждые 15 мин (96 тиков/день), не 5 мин.
    8 req × 96 = 768 req/день → неделя = 5 376. Всё ещё > 1000.

    ФИНАЛЬНОЕ РЕШЕНИЕ (R15): Phase 0 берет только 5m, 100 баров.
    Bybit kline endpoint = 1 req на 100 свечей = 1 req/pair/тик.
    8 req/тик. Cron каждые 15 мин = 96 тиков/день = 768/день = 5 376/week.
    Это 5.4x лимита. НЕДОПУСТИМО.

    АЛЬТЕРНАТИВА (R15 final): Phase 0 через ccxt fetch_ohlcv (public endpoint,
    НЕ linear derivatives API → НЕ считается в weekly quota).
    Public market data endpoints на Bybit = отдельный лимит, гораздо выше.

    РЕАЛИЗАЦИЯ: используем ccxt fetch_ohlcv (public, без API key для данных).
    ════════════════════════════════════════════════════════════════════════════

    Returns:
        {"status": "ok"|"error", "pairs": int, "bars": int, "error": str?}
    """
    print(f"── Phase 0: COLLECT 5m ({now_msk_str()}) ──", flush=True)
    try:
        from src.core.config import Config
        from src.core.database import DatabaseManager
        from src.core.logger import setup_logger

        config = Config.load()
        ct_logger = setup_logger('cryptotrader', level='INFO',
                                 log_file=config.logging.get('file'))
        db = DatabaseManager(config.postgresql, ct_logger)

        # v7a universe — динамически из STRATEGIES (single source of truth).
        # [Fix 19.07.2026 Босс] Добавлены новые пары, без этого списка Phase 0
        # собирает только 8 старых пар → scanner skip'ает stale data.
        from cryptotrader_strategies.clone5_multi_runner import STRATEGIES
        symbols = list(STRATEGIES[0]["symbols"])

        # R16 (17.06): откат 15m → 5m. Сравнение на идентичном окне 31д показало
        # PF 1.89 (5m) vs 1.02 (15m). 15m деградировал. Возврат на 5m.
        COLLECT_TF = "5m"

        # Используем ccxt public endpoint (не считается в linear quota)
        import ccxt
        ex = ccxt.bybit({
            'enableRateLimit': True,
            'options': {'defaultType': 'linear'},
        })

        from sqlalchemy import text as sa_text
        from datetime import datetime as _dt, timezone as _tz

        # ────────────────────────────────────────────────────────────────────
        # P0-FIX (15.07.2026): naive datetime → timezone-aware UTC
        #
        # ЗАЧЕМ: Postgres session timezone = Europe/Moscow (+03:00). Когда
        # Phase 0 шлёт naive datetime (без tzinfo), Postgres интерпретирует
        # его как local time и сохраняет с меткой +03:00. Реальный UTC-
        # timestamp от Bybit (например, 12:25 UTC = 15:25 MSK) превращается
        # в `12:25+03:00` = 09:25 UTC, что на 3 ЧАСА РАНЬШЕ реальности.
        # Сканер видит эти данные как stale → 0 сигналов.
        #
        # РЕШЕНИЕ: fromtimestamp(ts_ms, tz=timezone.utc) → aware UTC →
        # Postgres сохраняет корректно.
        #
        # ДОПОЛНИТЕЛЬНО: добавлен RETURNING (xmax=0) для подсчёта реальных
        # INSERT vs UPDATE (ON CONFLICT) — раньше логировались все 800 как
        # «bars», хотя фактически ни одного нового не вставлялось.
        # ────────────────────────────────────────────────────────────────────
        total_bars = 0
        total_inserted = 0
        total_updated = 0
        for sym in symbols:
            try:
                ohlcv = ex.fetch_ohlcv(sym, COLLECT_TF, limit=100)
                if not ohlcv:
                    continue
                from sqlalchemy import text as sa_text
                with db.get_session() as session:
                    for candle in ohlcv:
                        # FIX: timezone-aware UTC (was: _dt.utcfromtimestamp — naive)
                        ts = _dt.fromtimestamp(candle[0] / 1000, tz=_tz.utc)
                        o, h, l, c, v = candle[1], candle[2], candle[3], candle[4], candle[5]
                        qv = float(v) * float(c)
                        res = session.execute(sa_text("""
                            INSERT INTO ohlcv_raw
                                (exchange, symbol, timeframe, timestamp,
                                 open, high, low, close, volume, quote_volume, trades_count)
                            VALUES
                                (:ex, :sym, :tf, :ts, :o, :h, :l, :c, :v, :qv, 0)
                            ON CONFLICT (exchange, symbol, timeframe, timestamp)
                            DO UPDATE SET
                                open = EXCLUDED.open, high = EXCLUDED.high,
                                low = EXCLUDED.low, close = EXCLUDED.close,
                                volume = EXCLUDED.volume, quote_volume = EXCLUDED.quote_volume
                            RETURNING (xmax = 0) AS was_inserted
                        """), {
                            "ex": "bybit", "sym": sym, "tf": COLLECT_TF, "ts": ts,
                            "o": o, "h": h, "l": l, "c": c, "v": v, "qv": qv,
                        })
                        row = res.fetchone()
                        if row and row[0]:
                            total_inserted += 1
                        else:
                            total_updated += 1
                    session.commit()
                    total_bars += len(ohlcv)
            except Exception as e:
                log.warning(f"Phase 0: {sym} fetch failed: {e}")

        print(
            f"  Phase 0 done: {len(symbols)} pairs, {total_bars} bars "
            f"(inserted={total_inserted}, updated={total_updated})",
            flush=True,
        )
        return {
            "status": "ok",
            "pairs": len(symbols),
            "bars": total_bars,
            "inserted": total_inserted,
            "updated": total_updated,
        }
    except Exception as e:
        log.exception(f"Phase 0 (collect) failed: {e}")
        return {"status": "error", "pairs": 0, "bars": 0, "error": str(e)}


def main() -> int:
    """Единый collect→scan→execute tick. Возвращает exit code (0=ok, 1=error)."""
    started = datetime.now(timezone.utc)
    log.info(f"=== Collect+Scan+Execute tick: {started.isoformat()} ===")

    # ── PHASE 0: COLLECT (Bybit kline API, ~8 req) ──
    p0 = phase0_collect()

    # ── PHASE 1: SCAN (0 API calls) ──
    p1 = phase1_scan()

    # ── PHASE 2: EXECUTE (0-4 API calls) ──
    cb_state = _cb_load()
    p2 = phase2_execute(cb_state)

    # ── REPORT ──
    duration = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    summary = {
        "status": "ok" if p0.get("status") != "error" and p1.get("status") != "error" and p2.get("status") != "error" else "error",
        "ts": msk_iso_now(),
        "ts_msk": now_msk_str(),
        "duration_s": duration,
        "phase0_collect": p0,
        "phase1_scan": p1,
        "phase2_execute": p2,
    }
    log.info(
        f"Tick done in {duration}s: collect_pairs={p0.get('pairs', 0)} "
        f"scan_signals={p1.get('signals', 0)} "
        f"buys={p2.get('buys', 0)} sells={p2.get('sells', 0)} "
        f"sl_tp={p2.get('sl_tp', 0)} trailing={p2.get('trailing', 0)} "
        f"errors={p2.get('errors', 0)}"
    )
    print(json.dumps(summary, indent=2), flush=True)

    # Exit code: 0 even if Phase 0/1 had issues (non-fatal). Exit 1 only if
    # all three phases failed hard (extremely unlikely).
    errors = [p0.get("status"), p1.get("status"), p2.get("status")]
    if all(s == "error" for s in errors):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
