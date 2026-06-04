# CryptoTrader MCP Server

## Подключение к OpenCode

Добавь в `~/.config/opencode/opencode.json`:

```json
{
  "mcpServers": {
    "cryptotrader": {
      "command": "python",
      "args": ["C:\\Projects\\СryptoTrader\\src\\mcp_server.py"],
      "cwd": "C:\\Projects\\СryptoTrader",
      "env": {
        "PATH": "C:\\Projects\\СryptoTrader\\.venv\\Scripts;${env:PATH}"
      }
    }
  }
}
```

Для Linux:

```json
{
  "mcpServers": {
    "cryptotrader": {
      "command": "/opt/cryptotrader/.venv/bin/python",
      "args": ["/opt/cryptotrader/src/mcp_server.py"],
      "cwd": "/opt/cryptotrader"
    }
  }
}
```

## Доступные инструменты

| Инструмент | Описание |
|------------|----------|
| `collect_data` | Собрать OHLCV данные с бирж |
| `get_sentiment` | Получить агрегированный сентимент |
| `analyze_news` | Анализировать новости через LLM |
| `make_decision` | Сгенерировать сигнал для пары |
| `decide_all` | Решения для всех активных пар |
| `execute_trades` | Исполнить отложенные сигналы |
| `get_balance` | Баланс аккаунта |
| `get_positions` | Открытые позиции |
| `close_position` | Закрыть позицию |
| `get_signals` | История сигналов |
| `get_trades` | История сделок |
| `store_expert_analysis` | Сохранить экспертный анализ в RAGFlow |
| `get_expert_context` | Получить контекст из RAGFlow |
| `run_full_pipeline` | Полный цикл: collect→sentiment→decide→execute |
| `health_check` | Проверка здоровья всех компонентов |

## Примеры использования

```
# Проверить здоровье системы
Вызови health_check

# Собрать данные по BTC и ETH
Вызови collect_data с symbols=["BTCUSDT", "ETHUSDT"]

# Получить сентимент
Вызови get_sentiment с hours=24

# Принять решение по BTC
Вызови make_decision с symbol="BTCUSDT"

# Проверить открытые позиции
Вызови get_positions

# Закрыть позицию по BTC
Вызови close_position с symbol="BTCUSDT"

# Полный цикл
Вызови run_full_pipeline
```

## Установка зависимостей

```bash
cd C:\Projects\СryptoTrader
.venv\Scripts\pip install -r requirements.txt
```
