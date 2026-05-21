# CryptoTrader — Architecture Overview

## 1. Концепция системы

CryptoTrader — это multi-agent trading system на Python для автоматической торговли
криптовалютами через Binance и Bybit. Система собирает рыночные данные, анализирует
новостной сентимент, генерирует торговые сигналы (через LLM + rule-based стратегии)
и исполняет ордера.

## 2. Компоненты архитектуры

### 2.1. Ядро (Core)
- **Config** (singleton) — загрузка YAML + .env, инжект секретов
- **DatabaseManager** — PostgreSQL через SQLAlchemy, управление сессиями
- **Logger** — централизованный логгер

### 2.2. Агенты (Agents)
Все наследуются от `BaseAgent` (src/agents/base.py):

| Агент | Файл | Назначение |
|-------|------|------------|
| **DataCollectorAgent** | `data_collector.py` | Сбор OHLCV + новостей с бирж и RSS |
| **SentimentAgent** | `sentiment_agent.py` | Анализ сентимента новостей через LLM |
| **TradingDecisionAgent** | `trading_agent.py` | Генерация торговых решений (LLM) |
| **ExecutionAgent** | `execution_agent.py` | Исполнение ордеров, SL/TP, синк позиций |
| **TelegramNotifier** | `telegram_notifier.py` | Отправка уведомлений в Telegram |

```python
# BaseAgent — абстрактный базовый класс
class BaseAgent(ABC):
    def __init__(self, name: str, logger: logging.Logger):
        self.name = name
        self.logger = logger
        self.running = False
        self.last_run = None

    @abstractmethod
    def run_once(self) -> Dict[str, Any]:
        """Execute one cycle of the agent. Returns result dict."""
        pass
```

### 2.3. Стратегии (Rule-based Strategies)
Отдельный пакет `cryptotrader_strategies/`:

| Стратегия | Таймфреймы | Горизонт |
|-----------|-----------|----------|
| **ScalpingStrategy** | 1m, 5m | Секунды/минуты |
| **IntradayStrategy** | 15m, 1h, 4h | Часы |
| **PositionStrategy** | 4h, 1d, 1w | Дни/недели |

```python
from cryptotrader_strategies import ScalpingStrategy, IntradayStrategy, PositionStrategy
strategy = ScalpingStrategy()
signal = strategy.analyze(df_1m=df_1m, df_5m=df_5m, ...)
```

### 2.4. Шлюзы (Gateways)
- **BybitAPI** — V5 REST API (spot + linear perpetual)
- **BinanceAPI** — Spot REST API (testnet/mainnet)

### 2.5. API сервер
- FastAPI на порту 8000 (`src/api/server.py`), запускается через `main.py --task api`

## 3. Pipeline данных (data flow)

Полный pipeline состоит из 4 шагов:

### Step 1: Data Collection
```
DataCollectorAgent.run_once()
  ├── Выбор символов (top по volume)
  ├── Сбор OHLCV свечей (binance/bybit)
  ├── Сбор новостей (RSS + CryptoRank + CoinDesk)
  └── Запись в таблицы: ohlcv_raw, news_raw
```

### Step 2: Sentiment Analysis
```
SentimentAgent.run_once()
  ├── Чтение непроанализированных новостей из news_raw
  ├── LLM вызов (OpenRouter/Ollama) для оценки сентимента
  ├── Сохранение sentiment_score в news_raw
  └── Агрегация: средний сентимент, bullish ratio
```

### Step 3: Trading Decision
```
TradingDecisionAgent.run_once()
  ├── Чтение OHLCV из ohlcv_processed + news_raw
  ├── Вычисление технических индикаторов (RSI, MACD, BB, SMA, ATR)
  ├── LLM промпт с контекстом (рынок + сентимент)
  ├── Генерация сигнала (BUY/SELL/HOLD) с confidence
  └── Запись в таблицы: signals, decisions
```

### Step 4: Execution
```
ExecutionAgent.run_once()
  ├── Чтение PENDING сигналов из signals
  ├── Валидация (баланс, риск-менеджмент)
  ├── Отправка ордера через BybitAPI / BinanceAPI
  ├── Установка SL/TP
  ├── Синк позиций с биржи
  └── Запись в таблицы: trades, positions
```

### Полный pipeline (скрипт):

```python
# run_pipeline.py — последовательный запуск всех шагов
data_collector.run_once()       # [1/4] Collect data
sentiment.run_once()            # [2/4] Analyze sentiment
trading.run_once()              # [3/4] Generate decisions
executor.run_once()             # [4/4] Execute trades
```

## 4. Scheduler — циклический запуск

`CryptoTraderScheduler` (scheduler.py) запускает агентов по интервалам:

| Task | Интервал | Описание |
|------|----------|----------|
| collect | 600s (10 min) | Сбор данных |
| sentiment | 1800s (30 min) | Анализ сентимента |
| decide | 900s (15 min) | Генерация сигналов |
| execute | 300s (5 min) | Исполнение + проверка SL/TP |

```python
class CryptoTraderScheduler:
    intervals = {
        'collect': 600,
        'sentiment': 1800,
        'decide': 900,
        'execute': 300,
        'hourly_report': 3600,
    }

    def run(self):
        # Initial cycle
        for task in ['collect', 'sentiment', 'decide', 'execute']:
            self.run_task(task)
        # Main loop — checks every 30 seconds
        while self.running:
            for task in ['collect', 'sentiment', 'decide', 'execute']:
                if self.should_run(task):
                    self.run_task(task)
            time.sleep(30)
```

## 5. Взаимодействие агентов

```
                    ┌─────────────────┐
                    │   Config (YAML  │
                    │   + .env)       │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
       ┌──────▼──────┐ ┌────▼──────┐  ┌─────▼──────┐
       │Database     │ │Telegram   │  │Email       │
       │Manager      │ │Notifier   │  │Notifier    │
       └──────┬──────┘ └───────────┘  └────────────┘
              │
    ┌─────────┼────────────┬───────────────┐
    │         │            │               │
┌───▼────┐ ┌──▼──────┐ ┌───▼────────┐ ┌───▼──────┐
│Data    │ │Sentiment│ │Trading     │ │Execution │
│Collect │ │Agent    │ │Decision    │ │Agent     │
│Agent   │ │         │ │Agent       │ │          │
└───┬────┘ └─────────┘ └───┬────────┘ └───┬──────┘
    │                       │              │
    ▼                       ▼              ▼
BybitAPI             OpenRouter/Ollama   BybitAPI
BinanceAPI           RAGFlow Context     BinanceAPI
RSS Feeds                                 │
CryptoRank                                 ▼
CoinDesk                              Telegram
```

## 6. Точки входа

| Скрипт | Команда | Описание |
|--------|---------|----------|
| `main.py` | `python main.py --task all` | Полный pipeline (1 раз) |
| `main.py` | `python main.py --task collect` | Только сбор данных |
| `main.py` | `python main.py --task decide --symbols BTCUSDT` | Сигнал для конкретного символа |
| `main.py` | `python main.py --task api` | Запуск FastAPI сервера |
| `run_pipeline.py` | `python run_pipeline.py` | Полный pipeline (все шаги) |
| `scheduler.py` | `python scheduler.py` | Циклический запуск 24/7 |
| `db_cleanup.py` | `python db_cleanup.py` | Очистка старых данных |

## 7. Внешние сервисы

- **PostgreSQL** — хранение всех данных (192.168.0.194:5432)
- **OpenRouter** — LLM для сентимента и торговых решений
- **Ollama** — локальная альтернатива LLM (192.168.0.94:11434)
- **RAGFlow** — контекст для LLM (news + docs)
- **AnythingLLM** — альтернативный LLM интерфейс (192.168.0.133:3001)
- **Binance/Bitfinex/CoinEx** — биржи (через BinanceAPI)
- **Bybit** — основная биржа для исполнения
- **Telegram** — уведомления
- **Email (SMTP)** — email-уведомления
