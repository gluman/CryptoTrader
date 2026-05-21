# CryptoTrader — Алгоритм торговли

## Общая архитектура пайплайна

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        FULL PIPELINE (main.py --task all)               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ШАГ 1: DataCollectorAgent.run_once()                                  │
│  ├── select_symbols()           → фильтрация пар по критериям          │
│  ├── fetch_binance_klines()     → OHLCV с Binance (20 пар × 6 TF)    │
│  ├── fetch_bybit_klines()       → OHLCV с Bybit                       │
│  ├── fetch_bitfinex_candles()   → OHLCV с Bitfinex                    │
│  ├── fetch_rss_news()           → новости из 6 RSS-лент               │
│  └── save_ohlcv_to_db()         → запись в PostgreSQL (ohlcv_raw)     │
│  └── save_news_to_db()          → запись в PostgreSQL (news_raw)      │
│                                                                         │
│  ШАГ 2: SentimentAgent.run_once()                                      │
│  ├── get_unanalyzed_news()      → новости без sentiment_score          │
│  ├── analyze_sentiment()        → LLM (OpenRouter) оценивает -1..+1  │
│  ├── update_sentiment()         → запись в news_raw                    │
│  └── get_aggregated_sentiment() → средний сентимент за 24ч            │
│                                                                         │
│  ШАГ 3: TradingDecisionAgent.run_once()                                │
│  ├── Для каждой активной пары:                                         │
│  │   ├── get_ohlcv_data()       → чтение из PostgreSQL (200 свечей)   │
│  │   ├── calculate_indicators() → SMA, RSI, MACD, ATR, BB, CSS        │
│  │   ├── get_aggregated_sentiment() → средний сентимент                │
│  │   ├── get_recent_signals()   → последние сигналы (для контекста)    │
│  │   ├── build_prompt()         → формирование промта для LLM          │
│  │   ├── call_llm()             → OpenRouter → JSON решение            │
│  │   └── save_decision()        → запись в signals + decisions         │
│  └── Итого: {buys: N, sells: N, holds: N}                             │
│                                                                         │
│  ШАГ 4: ExecutionAgent.run_once()                                      │
│  ├── SELECT * FROM signals WHERE status='PENDING'                      │
│  ├── Для BUY сигналов:                                                 │
│  │   ├── get_balance('binance') → получить баланс USDT                │
│  │   ├── position_size = balance × risk% (по умолчанию 1%)            │
│  │   └── execute_spot_buy()     → ордер на бирже                      │
│  ├── save_trade_to_db()         → запись в trades                      │
│  └── update_signal_status()     → PENDING → EXECUTED                  │
│                                                                         │
│  ШАГ 5: TelegramNotifier (опционально)                                 │
│  └── notify_daily_summary()     → итог дня                             │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Детальное описание каждого блока

### 1. DataCollectorAgent — Сбор данных

**Вызывается:** `data_collector.run_once()`

**Что делает:**
1. `select_symbols()` — выбирает пары для торговли по критериям:
   - Объём 24ч > $50M USDT
   - Изменение за час > 2%
   - Только пары с котировкой в USDT
   - Результат: список из ~10-20 пар

2. `fetch_binance_klines(symbol, timeframe)` — получает OHLCV данные:
   - GET `/api/v3/klines` (публичный, без ключа)
   - Таймфреймы: 1m, 5m, 15m, 1h, 4h, 1d
   - 500 свечей на пару

3. `fetch_bybit_klines(symbol, timeframe)` — аналогично для Bybit:
   - GET `/v5/market/kline` (публичный)

4. `fetch_rss_news()` — парсит RSS-ленты:
   - ForkLog (RU), Bits.Media (RU)
   - CoinDesk, CoinTelegraph, Bitcoin Magazine, The Block (EN)
   - Последние 10 статей из каждого источника

5. `save_ohlcv_to_db()` — записывает в PostgreSQL:
   - Таблица `ohlcv_raw`
   - UNIQUE constraint по (exchange, symbol, timeframe, timestamp)
   - ON CONFLICT DO NOTHING (защита от дублей)

**Данные на выходе:**
```json
{
  "ohlcv_records": 2400,
  "news_records": 45,
  "symbols_selected": 18,
  "timestamp": "2026-03-31T12:00:00"
}
```

---

### 2. SentimentAgent — Анализ настроений

**Вызывается:** `sentiment.run_once()`

**Что делает:**
1. `get_unanalyzed_news()` — находит новости без `sentiment_score`
   - SELECT из news_raw WHERE sentiment_score IS NULL

2. `analyze_sentiment(title, summary)` — для каждой новости:
   - Отправляет промт в OpenRouter (Llama 3.3 70B)
   - Промт: "Rate sentiment from -1.0 (bearish) to +1.0 (bullish)"
   - Парсит число из ответа

3. `update_sentiment()` — записывает скор в news_raw

4. `get_aggregated_sentiment()` — агрегация:
   - Средний сентимент за 24ч
   - Доля бычьих новостей (sentiment > 0)
   - Количество проанализированных новостей

**Данные на выходе:**
```json
{
  "analyzed": 15,
  "aggregated": {
    "avg_sentiment": 0.35,
    "bullish_ratio": 0.65,
    "news_count": 42,
    "sample_titles": ["BlackRock ETF...", "Bitcoin surges..."]
  }
}
```

---

### 3. TradingDecisionAgent — Генерация сигналов (КЛЮЧЕВОЙ)

**Вызывается:** `trading.run_once()` или `trading.run_once_for_symbol(symbol)`

**Для каждой пары выполняет:**

#### 3.1. Расчёт индикаторов (`calculate_indicators`)

| Индикатор | Формула | Назначение |
|-----------|---------|------------|
| **SMA 20** | `close.rolling(20).mean()` | Краткосрочный тренд |
| **SMA 50** | `close.rolling(50).mean()` | Среднесрочный тренд |
| **RSI 14** | `100 - 100/(1+RS)` | Перекупленность/перепроданность |
| **MACD** | `EMA(12) - EMA(26)` | Импульс + пересечение сигнальной |
| **ATR 14** | `TR.rolling(14).mean()` | Волатильность (для SL/TP) |
| **Bollinger Bands** | `SMA(20) ± 2×STD(20)` | Уровни поддержки/сопротивления |
| **CSS** | `(MA.diff) / ATR` → нормализация | Сила валюты (ваш алгоритм) |
| **Volume Ratio** | `volume / volume_sma(20)` | Подтверждение объёмом |

#### 3.2. Формирование промта (`build_prompt`)

Промт содержит:
- Текущие значения всех индикаторов
- Агрегированный сентимент
- История последних 5 сигналов
- Правила принятия решений

#### 3.3. LLM принимает решение (`call_llm`)

Отправляет промт в OpenRouter, получает JSON:
```json
{
  "signal": "BUY",
  "confidence": 0.78,
  "reasoning": "CSS crossed 0.25 upward, bullish trend confirmed by SMA50"
}
```

#### 3.4. Сохранение решения (`save_decision`)

- INSERT в `signals` — торговый сигнал
- INSERT в `decisions` — полный лог LLM (входные/выходные данные, latency, tokens)

**Сигналы, которые генерируются:**

| Сигнал | Условие (из промта) |
|--------|---------------------|
| **BUY** | CSS ↑ через 0.20, RSI < 70, сентимент > 0, цена > SMA50 |
| **SELL** | CSS ↓ через -0.20, RSI > 30, сентимент < 0 |
| **HOLD** | Конфликт сигналов, низкая уверенность, ожидание |

**Фильтр уверенности:**
- Если `confidence < 0.6` → сигнал меняется на HOLD
- Порог настраивается в `settings.yaml` → `agents.trading_decision.min_confidence`

---

### 4. ExecutionAgent — Исполнение ордеров

**Вызывается:** `executor.run_once()`

**Что делает:**
1. SELECT из `signals` WHERE status='PENDING'
2. Для каждого сигнала:

| Тип сигнала | Действие |
|-------------|----------|
| BUY | `execute_spot_buy(symbol, amount, exchange)` |
| SELL | `execute_spot_sell(symbol, quantity, exchange)` (только если есть позиция) |
| HOLD | Пропуск |

3. `execute_spot_buy()` — вызывает API биржи:
   - **Binance:** POST `/api/v3/order` (Market BUY, quoteOrderQty)
   - **Bybit:** POST `/v5/order/create` (Spot, marketUnit=quoteCoin)
   - **Bitfinex:** POST `/auth/w/order/submit` (EXCHANGE MARKET)

4. `save_trade_to_db()` — записывает в `trades` таблицу
5. `update_signal_status()` — обновляет status → 'EXECUTED'

**Управление позицией:**
- Размер позиции: 1% от баланса USDT (настраивается)
- Минимальный ордер: $10 USDT
- Подтверждение на Mainnet: через Telegram или AnythingLLM

---

## Сигналы и индикаторы — сводка

### Источники данных для сигналов

| Источник | Что даёт | Вес |
|----------|----------|-----|
| **CSS (Currency Slope Strength)** | Пересечение уровня 0.20 → триггер | 30% |
| **RSI 14** | Фильтр: не покупать при >70, не продавать при <30 | 15% |
| **SMA 50** | Фильтр тренда: BUY только выше, SELL только ниже | 15% |
| **MACD** | Подтверждение импульса | 10% |
| **Bollinger Bands** | Уровни входа/выхода | 5% |
| **Новостной сентимент** | Усиление/ослабление сигнала | 15% |
| **Объём** | Подтверждение (volume_ratio > 1.5) | 10% |

### Правила генерации сигнала

```
IF CSS пересекает 0.20 снизу вверх:
    IF RSI < 70 AND цена > SMA50 AND сентимент > 0:
        → BUY (confidence = CSS_value × sentiment_boost)
    ELSE IF RSI >= 70:
        → HOLD (перекупленность)
    ELSE IF цена < SMA50:
        → HOLD (нисходящий тренд)

IF CSS пересекает -0.20 сверху вниз:
    IF RSI > 30 AND сентимент < 0:
        → SELL (confidence = |CSS_value| × sentiment_boost)
    ELSE IF RSI <= 30:
        → HOLD (перепроданность)

ELSE:
    → HOLD
```

### Гибридная формула уверенности

```
confidence = base_css_strength × sentiment_multiplier × volume_multiplier × trend_filter

где:
  base_css_strength = |CSS_value| / 0.3 (нормализация к уровню)
  sentiment_multiplier = 1.2 если sentiment > 0.3, иначе 0.8
  volume_multiplier = 1.1 если volume_ratio > 1.5, иначе 1.0
  trend_filter = 1.0 если цена > SMA50, иначе 0.5 (для BUY)
```

---

## FastAPI Endpoints (для AnythingLLM Custom Tools)

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/health` | GET | Проверка здоровья |
| `/tools/collect` | POST | Запустить сбор данных |
| `/tools/sentiment` | POST | Анализ сентимента |
| `/tools/decide` | POST | Генерация сигналов |
| `/tools/decide/run` | GET | Полный цикл решений |
| `/tools/execute/buy` | POST | Исполнить BUY |
| `/tools/execute/sell` | POST | Исполнить SELL |
| `/tools/balance` | GET | Баланс аккаунта |
| `/tools/export` | POST | Экспорт в xlsx/csv |

---

## Режимы запуска

```bash
# Полный пайплайн (все шаги последовательно)
python main.py --task all

# Только сбор данных
python main.py --task collect

# Только анализ сентимента
python main.py --task sentiment

# Решения для конкретных пар
python main.py --task decide --symbols BTCUSDT ETHUSDT

# Исполнение ордеров
python main.py --task execute

# FastAPI сервер (для AnythingLLM)
python main.py --task api
```

---

## Расписание работы

| Агент | Расписание | Режим |
|-------|------------|-------|
| DataCollector | Каждые 10 мин | Cron |
| Sentiment | Каждые 30 мин | Cron |
| TradingDecision | По событиям (когда новые данные) | Event-driven |
| Execution | При появлении PENDING сигналов | Event-driven |
| Telegram | При ошибках и сделках | Event-driven |

---

*Документация обновлена: 2026-03-31*
