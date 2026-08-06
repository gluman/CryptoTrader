#!/bin/bash
# Еженедельный подбор SL/TP/max_hold для стратегии v9 CTL.
#
# Заменил прежний weekly_per_pair_optim_v25.py (04.08.2026):
#   • тот падал — 27.07 по HTTP 401, 03.08 по таймауту 600с, результата не давал ни разу;
#   • и подбирал вход вместе с выходом, что на holdout давало хуже случайного выбора.
# Новый скрипт подбирает ТОЛЬКО SL/TP/max_hold при зафиксированном входе,
# требует подтверждения на train и valid, пишет исключительно в JSON-конфиг
# (боевые .py не трогает).
#
# Вызов: cron 3418df439a80, no-agent режим — не зависит от доступности LLM.
set -o pipefail

LOG_DIR="$HOME/.hermes/cron/output/weekly_v9"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/optim_$(date +%Y%m%d_%H%M%S).log"

cd /home/andy/CryptoTrader_main || exit 1
/home/andy/cryptotrader-venv/bin/python cryptotrader_strategies/weekly_v9_optim.py > "$LOG" 2>&1
rc=$?

# В Telegram — только итог, полный лог остаётся в файле
if [ $rc -eq 0 ]; then
    echo "✅ Еженедельный подбор v9 CTL выполнен"
else
    echo "❌ Еженедельный подбор v9 CTL упал (код $rc)"
fi
grep -E "персональные параметры|средний exp|записано|Traceback|Error" "$LOG" | tail -8
echo "лог: $LOG"
exit $rc
