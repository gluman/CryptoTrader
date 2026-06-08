#!/usr/bin/env python3
"""
execute_cron.py — периодический исполнитель сигналов для Clone5-контура.

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ
═══════════════════════════════════════════════════════════════════════════════

История: code review `code_review/CODE_REVIEW_2026-06-08.md`, замечание C2
"Нет cron-исполнителя для контура Clone5".

Проблема:
  • Сканер `clone5_multi_runner.py` (cron `e020bee290d2`, every 60m) пишет
    сигналы в `strategy_signals(action='BUY'/'SELL', status='pending')`.
  • Исполнитель `ExecutionAgent.run_once(market_type='linear')` умеет
    читать эти сигналы, открывать ордера, вести SL/TP/трейлинг,
    делать orphan-sync — НО его никто не вызывал.
  • API-сервер `main.py --task api` (PID 1127072) — это ТОЛЬКО uvicorn,
    внутреннего планировщика нет. Внешний планировщик 192.168.0.251
    дёргает `/tools/execute/run` только для ЛЕГАСИ-пайплайна, не для
    Clone5.

Следствие (до 08.06.2026): торговая цепь Clone5 была разомкнута.
Сигналы копились, позиции не открывались, SL/TP не вёлся.

═══════════════════════════════════════════════════════════════════════════════
ЧТО ДЕЛАЕТ
═══════════════════════════════════════════════════════════════════════════════

  1. Инициализирует `ExecutionAgent` (тот же singleton, что использует
     легаси-контур) с `Config.load()` + `DatabaseManager`.
  2. Вызывает `executor.run_once(market_type='linear')` — единичный цикл:
     - читает pending BUY/SELL сигналы из `strategy_signals`
     - открывает ордера (с реальной подписью Bybit через ccxt)
     - ведёт SL/TP (per-position) + трейлинг
     - делает orphan-sync (positions DB <-> Bybit, закрывает фантомы)
     - чистит expired signals (TTL=60 min для clone5_*)
  3. Возвращает JSON-сводку в stdout (для парсинга планировщиком Hermes):
     ```
     {"status": "ok", "duration_s": 8.2, "buys": 0, "sells": 0,
      "sl_tp": 0, "cleaned": 0, "trailing": 0, "errors": 0}
     ```
  4. Лог в stderr (logging.basicConfig → sys.stderr).

═══════════════════════════════════════════════════════════════════════════════
ПОЧЕМУ ИМЕННО ТАК, А НЕ ИНАЧЕ
═══════════════════════════════════════════════════════════════════════════════

  1. **Свой процесс на каждый тик (не long-running daemon)**:
     Сканер `clone5_multi_cron.py` работает так же — cron поднимает
     свежий Python-процесс каждые 60 мин. Преимущества:
       - Crash одного тика не валит весь пайплайн (Process restart — это
         естественный watchdog).
       - Логи и stdout каждого тика — отдельный файл в
         `~/.hermes/cron/output/<job_id>/<timestamp>.md` (история).
       - Нет долгоживущих соединений с БД / Bybit (нет утечек сокетов).
     Недостаток: каждый тик платит за import time (~1s) + DB init
     (~0.5s) + ccxt init (~0.5s). На 3-мин cadence это приемлемо.

  2. **schedule: every 3m** (не 1m, не 5m):
     - 1m: слишком часто для scalping, нагрузка на Bybit (1800 req/h
       минимум, даже с 0 сигналов ccxt делает healthcheck).
     - 5m: на волатильном рынке SL/TP может опоздать на 1-3 бара (5-15 мин
       при 5m TF) — пропустим защитный выход.
     - 3m: компромисс. 20 req/h × 24h = 480 req/сутки — мизер. SL/TP
       опоздание ≤ 3 мин — на грани, но терпимо для $5/1x размера.
     - TTL для clone5_* в `_clean_expired_signals` = 60 мин = 20 тиков:
       сигнал НИКОГДА не пропадёт из-за того, что исполнитель не успел.

  3. **mode = no_agent=True, script = execute_cron.sh**:
     Согласно cronjob tool: "no_agent=True: script IS the job".
     Планировщик запускает скрипт, его stdout — это отчёт. LLM не
     привлекается. Это правильный путь для периодических скриптовых
     задач: 0 токенов, 0 model override, нет галлюцинаций.

  4. **deliver=local** (не telegram):
     Результат `0 buys, 0 sells, 0 errors` каждые 3 мин — шум. Босс
     получает результаты через `multi_strategy_monitor.sh` (каждые 30 мин
     в Telegram) и `compound_dashboard.sh` (каждые 6 часов). Не дублируем
     в Telegram.

  5. **Сначала `from src.core.config import Config`**:
     Тот же Config singleton, что использует `main.py` API-сервер.
     Гарантирует консистентность конфига между сканером → исполнителем
     → API.

  6. **JSON в stdout + log в stderr**:
     Планировщик Hermes парсит stdout как JSON-результат. Логи через
     logging.basicConfig → stderr (не мешают парсеру). При ошибке
     Exception → status="error" + error="..." (НЕ raise, чтобы cron
     пометил как FAILED, а не как crashed process).

  7. **try/except ВЕЗДЕ**:
     Главный try/except в main() ловит любые ошибки инициализации
     (Config, DB, ExecutionAgent). Внутри ExecutionAgent.run_once()
     есть свои try/except для каждой пары/позиции. Если одна пара
     упала — остальные продолжают работать.

═══════════════════════════════════════════════════════════════════════════════
РАЗВЁРТЫВАНИЕ
═══════════════════════════════════════════════════════════════════════════════

  Шаг 1: положить wrapper в место, где Hermes его найдёт:
    $ cp /home/andy/CryptoTrader_main/execute_cron.sh \\
        /home/andy/.hermes/scripts/execute_cron.sh
    $ chmod +x /home/andy/.hermes/scripts/execute_cron.sh

  ⚠️ ВАЖНО: Hermes для script-джобов (no_agent=True) ищет скрипт
  ТОЛЬКО в `~/.hermes/scripts/`. Если оставить в `CryptoTrader_main/`
  — cron будет падать с `Script not found`. См. §0.2 code review.

  Шаг 2: зарегистрировать cron:
    cronjob(action='create', job_id='ce83cb821455',
            schedule='every 3m', script='execute_cron.sh',
            deliver='local', workdir='/home/andy/CryptoTrader_main',
            no_agent=True)

  Шаг 3: проверить, что тики идут:
    $ ls -la ~/.hermes/cron/output/ce83cb821455/  # last 30 .md файлов
    $ tail -50 ~/.hermes/cron/output/ce83cb821455/<latest>.md

  Ожидаемый результат (при пустом рынке):
    Script Output: status: ok, duration_s: ~8, buys=0, sells=0, ...

  Ожидаемый результат (при найденном сетапе):
    Script Output: status: ok, duration_s: ~10, buys=1, ...

═══════════════════════════════════════════════════════════════════════════════
ИЗВЕСТНЫЕ ПРОБЛЕМЫ И ПЛАН ДЕЙСТВИЙ
═══════════════════════════════════════════════════════════════════════════════

  • Bybit 1310 (Weekly/Monthly OHLCV Limit Exhausted) — активен до
    2026-06-10 00:31 UTC. В это окно `fetch_ticker`/`fetch_balance`
    внутри ExecutionAgent могут падать. Cron тик будет FAILED в части
    `## Error`, но `Script Output` всё равно покажет `status: ok` (если
    ccxt error был пойман внутри run_once). После reset 10.06 —
    автоматически заработает.

  • Bybit 10002 (timestamp mismatch) — лечится через bybit_safe.py
    (adjustForTimeDifference + load_time_difference) + H6
    _bybit_timestamp() в ExecutionAgent. До 08.06 14:50 был
    «магический» -1500ms fallback; сейчас 4-уровневый.

  • Сканер `clone5_multi_runner.py` (cron e020bee290d2, every 60m) →
    `clone5_multi_cron.py` → пишет signals → этот исполнитель их
    подхватывает в течение 0-3 мин. **Цепь замкнута.**

═══════════════════════════════════════════════════════════════════════════════
СМ. ТАКЖЕ
═══════════════════════════════════════════════════════════════════════════════

  • code_review/CODE_REVIEW_2026-06-08.md, §0.4 R1 (C2 фикс)
  • code_review/CODE_REVIEW_2026-06-08.md, §0.4 R3 (H6 фикс)
  • code_review/CODE_REVIEW_2026-06-08.md, §C1 (action='BUY'/'SELL')
  • cryptotrader_strategies/clone5_multi_runner.py — producer сигналов
  • cryptotrader_strategies/clone5_multi_cron.py — wrapper сканера
  • src/agents/execution_agent.py — consumer (run_once)
  • bybit_safe.py — общий ccxt-хелпер с time-sync
"""
from __future__ import annotations

import os
import sys
import json
import logging
from datetime import datetime, timezone

# Подключаем корень проекта /home/andy/CryptoTrader, чтобы импорты
# `from src.core.config import Config` работали так же, как в main.py
sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

# Логи в stderr (stdout — для JSON-результата планировщика)
logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO'),
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stderr,
)
log = logging.getLogger('execute_cron')


def main() -> int:
    """Один цикл исполнения. Возвращает exit code (0=ok, 1=error)."""
    started = datetime.now(timezone.utc)
    log.info(f"=== Execute tick: {started.isoformat()} ===")
    try:
        from src.core.config import Config
        from src.core.database import DatabaseManager
        from src.core.logger import setup_logger
        from src.agents import ExecutionAgent

        # Тот же Config + DatabaseManager что и в main.py --task api.
        # Это гарантирует, что сканер, исполнитель и API видят
        # одинаковые настройки/БД/лог-файл.
        config = Config.load()
        ct_logger = setup_logger('cryptotrader', level='INFO',
                                  log_file=config.logging.get('file'))
        db = DatabaseManager(config.postgresql, ct_logger)
        executor = ExecutionAgent(config, ct_logger, db)

        # Главный вызов: pending signals → orders → SL/TP/trailing
        result = executor.run_once(market_type='linear')

        # JSON для парсинга планировщиком (Hermes читает stdout).
        # Ключи подобраны так, чтобы монитор и человек читали одинаково.
        summary = {
            "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "duration_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
            "buys": result.get('buys_executed', 0),
            "sells": result.get('sells_executed', 0),
            "sl_tp": result.get('sl_tp_triggered', 0),
            "cleaned": result.get('signals_cleaned', 0),
            "trailing": result.get('trailing_updated', 0),
            "errors": result.get('errors', 0),
        }
        log.info(
            f"Result: buys={summary['buys']} sells={summary['sells']} "
            f"sl_tp={summary['sl_tp']} cleaned={summary['cleaned']} "
            f"errors={summary['errors']} dur={summary['duration_s']}s"
        )
        print(json.dumps(summary, indent=2), flush=True)
        return 0
    except Exception as e:
        # Любая ошибка (Config/DB/ExecutionAgent) → status=error.
        # НЕ raise — чтобы cron записал FAILED в "## Error", но
        # процесс завершился штатно (без дампа).
        log.exception(f"Execute tick failed: {e}")
        error_summary = {
            "status": "error",
            "error": str(e),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(error_summary, indent=2), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
