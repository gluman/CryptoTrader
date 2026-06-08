#!/usr/bin/env python3
"""execute_cron.py — периодический исполнитель сигналов для контура Clone5.

Запускает ExecutionAgent.run_once(market_type='linear') каждые N минут.
Обрабатывает:
  - pending BUY/SELL сигналы из strategy_signals (от clone5_multi_cron.py)
  - SL/TP/trailing на открытых позициях
  - orphan-sync (positions DB <-> Bybit)

Cron: каждые 3 минуты (подхватывает сигналы в течение жизни одного скана сканера,
SL/TP требует частого цикла, иначе пропустим выход).

См. code_review/CODE_REVIEW_2026-06-08.md, замечание C2.
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone

sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

# logging
logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO'),
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger('execute_cron')


def main():
    started = datetime.now(timezone.utc)
    log.info(f"=== Execute tick: {started.isoformat()} ===")
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
        # Краткий JSON для парсинга
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
        log.info(f"Result: buys={summary['buys']} sells={summary['sells']} "
                 f"sl_tp={summary['sl_tp']} cleaned={summary['cleaned']} "
                 f"errors={summary['errors']} dur={summary['duration_s']}s")
        print(json.dumps(summary, indent=2), flush=True)
    except Exception as e:
        log.exception(f"Execute tick failed: {e}")
        print(json.dumps({
            "status": "error",
            "error": str(e),
            "ts": datetime.now(timezone.utc).isoformat(),
        }, indent=2), flush=True)


if __name__ == "__main__":
    main()
