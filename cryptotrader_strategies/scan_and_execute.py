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

def main() -> int:
    """Единый scan→execute tick. Возвращает exit code (0=ok, 1=error)."""
    started = datetime.now(timezone.utc)
    log.info(f"=== Scan+Execute tick: {started.isoformat()} ===")

    # ── PHASE 1: SCAN (0 API calls) ──
    p1 = phase1_scan()

    # ── PHASE 2: EXECUTE (0-4 API calls) ──
    cb_state = _cb_load()
    p2 = phase2_execute(cb_state)

    # ── REPORT ──
    duration = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    summary = {
        "status": "ok" if p1.get("status") != "error" and p2.get("status") != "error" else "error",
        "ts": msk_iso_now(),
        "ts_msk": now_msk_str(),
        "duration_s": duration,
        "phase1_scan": p1,
        "phase2_execute": p2,
    }
    log.info(
        f"Tick done in {duration}s: scan_signals={p1.get('signals', 0)} "
        f"buys={p2.get('buys', 0)} sells={p2.get('sells', 0)} "
        f"sl_tp={p2.get('sl_tp', 0)} trailing={p2.get('trailing', 0)} "
        f"errors={p2.get('errors', 0)}"
    )
    print(json.dumps(summary, indent=2), flush=True)

    # Exit code: 0 even if Phase 1 had 0 signals (that's normal, not error).
    # Exit 1 only if both phases failed hard.
    if p1.get("status") == "error" and p2.get("status") == "error":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
