# CryptoTrader — Database Schema

## Обзор

Система использует **PostgreSQL** (база `cryptotrader`) с SQLAlchemy ORM.
Управление — через класс `DatabaseManager` (src/core/database.py).

```python
# Подключение (из config)
connection_string = f"postgresql://{config['username']}:***@{config['host']}:{config['port']}/{config['database']}"
# pool_size=10, max_overflow=20
```

## 1. Таблицы

### 1.1. ohlcv_raw — сырые OHLCV свечи

```python
class OHLCVRaw(Base):
    __tablename__ = 'ohlcv_raw'
```

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL PK | Первичный ключ |
| exchange | VARCHAR(50) NOT NULL | Биржа (binance, bybit) |
| symbol | VARCHAR(20) NOT NULL | Пара (BTCUSDT) |
| timeframe | VARCHAR(10) NOT NULL | 1m, 5m, 15m, 1h, 4h, 1d |
| timestamp | TIMESTAMPTZ NOT NULL | Время свечи |
| open | NUMERIC(20,8) NOT NULL | Цена открытия |
| high | NUMERIC(20,8) NOT NULL | Максимум |
| low | NUMERIC(20,8) NOT NULL | Минимум |
| close | NUMERIC(20,8) NOT NULL | Цена закрытия |
| volume | NUMERIC(30,8) NOT NULL | Объем |
| quote_volume | NUMERIC(30,8) | Объем в quote |
| trades_count | INTEGER | Кол-во сделок |
| created_at | TIMESTAMPTZ | Когда записано |

**Unique constraint**: `uix_ohlcv_unique` на `(exchange, symbol, timeframe, timestamp)` — для `ON CONFLICT DO NOTHING`.

### 1.2. ohlcv_processed — обработанные свечи с индикаторами

```python
class OHLCVProcessed(Base):
    __tablename__ = 'ohlcv_processed'
```

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL PK | |
| exchange, symbol, timeframe, timestamp | как в ohlcv_raw | |
| open … volume | NUMERIC | |
| sma_20, sma_50, sma_200 | NUMERIC(20,8) | Простые скользящие средние |
| ema_12, ema_26 | NUMERIC(20,8) | Экспоненциальные скользящие |
| rsi_14 | NUMERIC(10,4) | RSI |
| macd, macd_signal, macd_hist | NUMERIC(20,8) | MACD |
| atr_14 | NUMERIC(20,8) | ATR |
| bollinger_upper, bollinger_middle, bollinger_lower | NUMERIC(20,8) | Bollinger Bands |
| css_value, css_prior | NUMERIC(10,4) | CSS indicator |
| volume_sma_20 | NUMERIC(30,8) | SMA объема |
| volume_ratio | NUMERIC(10,4) | Отношение объема к SMA |

### 1.3. news_raw — новости

```python
class NewsRaw(Base):
    __tablename__ = 'news_raw'
```

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL PK | |
| source | VARCHAR(100) NOT NULL | Источник (RSS, CryptoRank, CoinDesk) |
| title | TEXT NOT NULL | Заголовок |
| url | TEXT UNIQUE NOT NULL | Ссылка |
| published_at | TIMESTAMPTZ NOT NULL | Дата публикации |
| summary | TEXT | Краткое содержание |
| language | VARCHAR(10) DEFAULT 'en' | Язык |
| sentiment_score | NUMERIC(3,2) | Оценка сентимента (-1.0 … 1.0) |
| sentiment_source | VARCHAR(50) | Откуда сентимент (LLM) |
| ragflow_document_id | VARCHAR(255) | ID в RAGFlow |

### 1.4. signals — торговые сигналы (основные)

```python
class Signal(Base):
    __tablename__ = 'signals'
```

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL PK | |
| symbol | VARCHAR(20) NOT NULL | |
| exchange | VARCHAR(50) NOT NULL | |
| market_type | VARCHAR(20) DEFAULT 'spot' | spot / linear |
| timeframe | VARCHAR(10) NOT NULL | |
| timestamp | TIMESTAMPTZ NOT NULL | |
| signal_type | VARCHAR(10) NOT NULL | BUY / SELL / HOLD |
| strength | NUMERIC(5,4) NOT NULL | Сила сигнала |
| css_value | NUMERIC(10,4) | CSS indicator |
| rsi_14 | NUMERIC(10,4) | |
| macd | NUMERIC(20,8) | |
| atr_14 | NUMERIC(20,8) | |
| price | NUMERIC(20,8) | Цена в момент сигнала |
| sentiment_score | NUMERIC(3,2) | |
| news_volume | INTEGER DEFAULT 0 | |
| volume_24h | NUMERIC(30,8) | |
| confidence | NUMERIC(5,4) | Итоговая уверенность |
| model_version | VARCHAR(50) | Версия LLM |
| reasoning | TEXT | Обоснование |
| status | VARCHAR(20) DEFAULT 'PENDING' | PENDING, EXECUTED, FAILED, CANCELLED |
| executed_at | TIMESTAMPTZ | |
| pnl_percent | NUMERIC(10,4) | |
| pnl_absolute | NUMERIC(20,8) | |
| ragflow_decision_id | VARCHAR(255) | |

### 1.5. strategy_signals — сигналы от rule-based стратегий

```python
class StrategySignal(Base):
    __tablename__ = 'strategy_signals'
```

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL PK | |
| symbol | VARCHAR(20) NOT NULL INDEX | |
| strategy | VARCHAR(20) NOT NULL INDEX | Scalping, Intraday, Position |
| action | VARCHAR(10) NOT NULL | BUY / SELL / HOLD |
| confidence | NUMERIC(5,4) NOT NULL | |
| entry_price | NUMERIC(20,8) | Цена входа |
| stop_loss | NUMERIC(20,8) | Стоп-лосс |
| take_profit | NUMERIC(20,8) | Тейк-профит |
| timeframes | VARCHAR(50) | Через запятую (1m,5m) |
| reasoning | TEXT | |
| status | VARCHAR(20) DEFAULT 'pending' | pending, executed, cancelled |
| executed_at | TIMESTAMPTZ | |
| pnl_percent, pnl_absolute | NUMERIC | |
| exchange | VARCHAR(50) DEFAULT 'binance' | |

### 1.6. decisions — LLM решения (логгинг)

```python
class Decision(Base):
    __tablename__ = 'decisions'
```

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL PK | |
| signal_id | BIGINT | FK на signals.id |
| timestamp | TIMESTAMPTZ NOT NULL | |
| market_data_json | JSON NOT NULL | Рыночные данные |
| sentiment_data_json | JSON | Сентимент |
| ragflow_context | TEXT | Контекст из RAGFlow |
| news_context | TEXT | |
| llm_model | VARCHAR(100) NOT NULL | Модель LLM |
| prompt_tokens, completion_tokens, total_tokens | INTEGER | Токены |
| latency_ms | INTEGER | Время ответа |
| decision_json | JSON NOT NULL | Итоговое решение |

### 1.7. trades — исполненные сделки

```python
class Trade(Base):
    __tablename__ = 'trades'
```

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL PK | |
| signal_id | BIGINT | FK |
| exchange, symbol | VARCHAR | |
| side | VARCHAR(10) NOT NULL | BUY / SELL |
| order_type | VARCHAR(20) NOT NULL | MARKET / LIMIT |
| quantity | NUMERIC(30,8) NOT NULL | |
| price | NUMERIC(20,8) | |
| fee | NUMERIC(20,8) | |
| pnl_percent, pnl_absolute | NUMERIC | |
| order_id, order_link_id | VARCHAR(255) | ID ордера на бирже |
| stop_loss, take_profit | NUMERIC(20,8) | |
| created_at, updated_at | TIMESTAMPTZ | |

### 1.8. positions — открытые/закрытые позиции

```python
class Position(Base):
    __tablename__ = 'positions'
```

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL PK | |
| symbol, exchange | VARCHAR | |
| market_type | VARCHAR(20) DEFAULT 'spot' | spot / linear |
| side | VARCHAR(10) DEFAULT 'LONG' | LONG / SHORT |
| entry_price | NUMERIC(20,8) NOT NULL | |
| quantity | NUMERIC(30,8) NOT NULL | |
| cost_usdt | NUMERIC(20,8) NOT NULL | |
| stop_loss, take_profit | NUMERIC(20,8) | |
| trailing_stop_activated | BOOLEAN DEFAULT False | |
| trailing_stop_price | NUMERIC(20,8) | |
| highest_price, lowest_price | NUMERIC(20,8) | |
| unrealized_pnl, unrealized_pnl_percent | NUMERIC | |
| status | VARCHAR(20) DEFAULT 'OPEN' | OPEN / CLOSED |
| opened_at, closed_at | TIMESTAMPTZ | |
| close_price | NUMERIC(20,8) | |
| realized_pnl, realized_pnl_percent | NUMERIC | |
| signal_id, trade_id | BIGINT | |
| notes | TEXT | |
| leverage | INTEGER DEFAULT 1 | |

### 1.9. selected_symbols — отобранные символы

```python
class SelectedSymbol(Base):
    __tablename__ = 'selected_symbols'
```

| Колонка | Тип |
|---------|-----|
| id | BIGSERIAL PK |
| symbol, exchange | VARCHAR |
| volume_24h | NUMERIC(30,8) |
| change_1h | NUMERIC(10,4) |
| spread_percent | NUMERIC(10,4) |
| selection_score | NUMERIC(10,4) |
| is_active | BOOLEAN DEFAULT True |
| selected_at | TIMESTAMPTZ |

### 1.10. agent_logs — логи агентов

```python
class AgentLog(Base):
    __tablename__ = 'agent_logs'
```

| Колонка | Тип |
|---------|-----|
| id | BIGSERIAL PK |
| agent_name | VARCHAR(50) NOT NULL |
| level | VARCHAR(10) NOT NULL (INFO/ERROR/WARNING) |
| message | TEXT NOT NULL |
| data_json | JSON |
| timestamp | TIMESTAMPTZ |

### 1.11. export_history — история экспорта

```python
class ExportHistory(Base):
    __tablename__ = 'export_history'
```

| Колонка | Тип |
|---------|-----|
| id | BIGSERIAL PK |
| export_type | VARCHAR(20) NOT NULL |
| file_path | TEXT NOT NULL |
| records_count | INTEGER |
| created_at | TIMESTAMPTZ |

## 2. Retention periods (db_cleanup.py)

```python
DEFAULTS = {
    'signals':         7,   # signals
    'strategy_signals': 7,  # strategy_signals
    'trades':          30,  # trades
    'decisions':       7,   # decisions
    'news_raw':        7,   # news_raw
    'agent_logs':      7,   # agent_logs
    'positions':       90,  # positions (только CLOSED!)
    'ohlcv_raw':       30,  # ohlcv_raw (опционально, --all)
    'ohlcv_processed': 30,  # ohlcv_processed (опционально, --all)
    'export_history':  30,  # export_history
}
```

```bash
# Использование
python db_cleanup.py                          # Дефолтная очистка
python db_cleanup.py --dry-run                # Preview
python db_cleanup.py --all                    # + очистка OHLCV
python db_cleanup.py --keep-signals 14        # Кастомные retention
```

**Важно**: PENDING сигналы НЕ удаляются. OPEN позиции НЕ удаляются (safety check).

## 3. Связи между таблицами

```
signals ──→ decisions    (signal_id)
signals ──→ trades       (signal_id)
trades  ──→ positions    (trade_id)
signals ──→ positions    (signal_id)
```

## 4. DatabaseManager API

```python
db = DatabaseManager(config.postgresql, logger)
db.create_tables()           # Создание всех таблиц + constraints
db.drop_tables()             # DROP ALL (осторожно!)
db.test_connection()         # Проверка соединения
db.get_session()             # Context manager для сессии (auto commit/rollback)
```
