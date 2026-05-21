# CryptoTrader — Exchange Gateways & External Services

## 1. Обзор шлюзов

Система взаимодействует с криптобиржами через два основных API-шлюза.
Все шлюзы находятся в `src/gateways/`.

## 2. BybitAPI (V5 REST API)

```python
# src/gateways/bybit_api.py
class BybitAPI:
    """Bybit V5 REST API Wrapper with rate limiting and signing"""
```

### Инициализация

```python
from src.gateways.bybit_api import BybitAPI

bybit = BybitAPI(
    api_key=config.bybit['api_key'],
    api_secret=config.bybit['api_secret'],
    testnet=True,                    # testnet.bybit.com
    recv_window=5000,                # ms
    logger=logger
)
```

### Rate limiting

```python
MIN_INTERVAL_GET = 0.100   # 100ms между GET
MIN_INTERVAL_POST = 0.300  # 300ms между POST
```

Автоматическое соблюдение: `_ensure_rate_limit(is_post)` — sleep перед запросом.

### Market Data

| Метод | Endpoint | Описание |
|-------|----------|----------|
| `get_tickers(category, symbol)` | `/v5/market/tickers` | Все тикеры или по символу |
| `get_ticker(symbol)` | `/v5/market/tickers` | Один символ (spot → linear) |
| `get_kline(symbol, interval, category, limit)` | `/v5/market/kline` | Свечи (OHLCV) |
| `get_orderbook(symbol, category, limit)` | `/v5/market/orderbook` | Стакан |
| `get_instruments_info(category, symbol)` | `/v5/market/instruments-info` | Информация об инструменте |

### Account

| Метод | Описание |
|-------|----------|
| `get_wallet_balance(account_type='UNIFIED')` | Баланс кошелька |
| `get_account_info()` | Информация об аккаунте |

### Lot Size Helpers

```python
# Кэширование lot size фильтров
_lot_size_cache: Dict[str, dict] = {}

def get_lot_size_filter(self, symbol, category='spot'):
    """Получить фильтр размера для символа (закэшировано)"""

def round_qty_by_lot_size(self, symbol, qty, category='spot'):
    """Округлить количество до валидного шага (round-down!)"""
```

### Trading

| Метод | Описание |
|-------|----------|
| `create_order(category, symbol, side, order_type, qty, ...)` | Создание ордера |
| `create_spot_buy(symbol, qty, order_type='Market')` | Спот покупка |
| `create_spot_sell(symbol, qty, order_type='Market')` | Спот продажа |
| `create_linear_long(symbol, qty, leverage='1', ...)` | Linear long (с установкой leverage) |
| `create_linear_short(symbol, qty, leverage='1', ...)` | Linear short |
| `cancel_order(category, symbol, order_id)` | Отмена ордера |
| `cancel_all_orders(category, symbol)` | Отмена всех ордеров |
| `get_open_orders(category, symbol)` | Открытые ордера |
| `get_order_history(category, symbol, limit)` | История ордеров |

### Position (Derivatives)

| Метод | Описание |
|-------|----------|
| `get_positions(category='linear', symbol)` | Открытые позиции |
| `set_leverage(symbol, buy_leverage, sell_leverage)` | Установка плеча |
| `set_tp_sl_mode(symbol, tpsl_mode='Full')` | Режим TP/SL |
| `set_trade_stop(category, symbol, side, ...)` | Установка SL и TP |
| `set_stop_loss(category, symbol, side, stop_loss)` | Только SL |
| `set_take_profit(category, symbol, side, take_profit)` | Только TP |

### Подпись запросов (HMAC-SHA256)

```python
def _sign(self, timestamp: str, param_str: str) -> str:
    sign_str = f"{timestamp}{self.api_key}{self.recv_window}{param_str}"
    return hmac.new(
        self.api_secret.encode('utf-8'),
        sign_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
```

Headers: `X-BAPI-API-KEY`, `X-BAPI-TIMESTAMP`, `X-BAPI-SIGN`, `X-BAPI-RECV-WINDOW`.

### Проверка синхронизации часов

```python
bybit.check_clock_sync()  # Допустимое расхождение: 5 секунд
```

## 3. BinanceAPI (Spot REST API)

```python
# src/gateways/binance_api.py
class BinanceAPI:
    """Binance Spot REST API Wrapper with rate limiting and signing"""
```

### Инициализация

```python
from src.gateways.binance_api import BinanceAPI

binance = BinanceAPI(
    api_key=config.binance['api_key'],
    api_secret=config.binance['api_secret'],
    testnet=True,                    # testnet.binance.vision
    logger=logger
)
```

### Rate limiting

```python
MIN_INTERVAL_GET = 0.050   # 50ms между GET
MIN_INTERVAL_POST = 0.200  # 200ms между POST
```

### Market Data

| Метод | Endpoint | Описание |
|-------|----------|----------|
| `get_ticker(symbol)` | `/api/v3/ticker/24hr` | 24h тикер |
| `get_all_tickers()` | `/api/v3/ticker/24hr` | Все тикеры |
| `get_book_ticker(symbol)` | `/api/v3/ticker/bookTicker` | Лучшие bid/ask |
| `get_klines(symbol, interval, limit, start_time, end_time)` | `/api/v3/klines` | Свечи |
| `get_depth(symbol, limit)` | `/api/v3/depth` | Стакан |
| `get_exchange_info()` | `/api/v3/exchangeInfo` | Инфо о бирже |
| `get_symbol_info(symbol)` | `/api/v3/exchangeInfo` | Инфо о символе |

### Account

```python
def get_account(self): ...          # Информация об аккаунте
def get_balances(self): ...         # Ненулевые балансы
```

### Trading

| Метод | Описание |
|-------|----------|
| `create_order(symbol, side, type, quantity, price, ...)` | Создание ордера |
| `create_market_buy(symbol, quote_order_qty)` | Рыночная покупка (на сумму quote) |
| `create_market_sell(symbol, quantity)` | Рыночная продажа (по quantity base) |
| `create_limit_buy(symbol, quantity, price)` | Лимитная покупка |
| `create_limit_sell(symbol, quantity, price)` | Лимитная продажа |
| `cancel_order(symbol, order_id)` | Отмена ордера |
| `get_order(symbol, order_id)` | Статус ордера |
| `get_open_orders(symbol)` | Открытые ордера |
| `cancel_all_orders(symbol)` | Отмена всех ордеров |
| `get_my_trades(symbol, limit)` | История сделок |

### Подпись запросов (HMAC-SHA256)

```python
def _sign(self, params: Dict) -> str:
    query_string = urlencode(params)
    signature = hmac.new(
        self.api_secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature
```

Header: `X-MBX-APIKEY`.

### Форматирование quantity/price

```python
def format_quantity(self, symbol: str, quantity: float) -> str:
    # Использует LOT_SIZE filter stepSize

def format_price(self, symbol: str, price: float) -> str:
    # Использует PRICE_FILTER tickSize
```

### Проверка синхронизации часов

```python
binance.check_clock_sync()  # Допустимое расхождение: 5 секунд
```

## 4. Config — централизованная конфигурация

```python
# src/core/config.py
class Config:
    """Configuration loader for CryptoTrader with .env support"""
    _instance = None  # Singleton
```

### Загрузка

```python
# 1. Загрузка .env (все API ключи)
# 2. Загрузка config/settings.yaml
# 3. Инжект секретов из .env в config
config = Config.load()
```

### Свойства конфигурации

| Свойство | Тип | Источник |
|----------|-----|----------|
| `config.binance` | dict | YAML + .env |
| `config.bybit` | dict | YAML + .env |
| `config.bitfinex` | dict | YAML + .env |
| `config.postgresql` | dict | YAML + .env |
| `config.openrouter` | dict | YAML + .env |
| `config.telegram` | dict | YAML + .env |
| `config.cryptorank` | dict | YAML + .env |
| `config.coindesk` | dict | YAML + .env |
| `config.ragflow` | dict | YAML + .env |
| `config.anythingllm` | dict | YAML + .env |
| `config.ollama` | dict | YAML + .env |
| `config.timeframes` | list | YAML |
| `config.rss_feeds` | list | YAML |
| `config.agents` | dict | YAML |
| `config.logging` | dict | YAML |
| `config.selection_criteria` | dict | YAML |
| `config.css_indicator` | dict | YAML |

### Доступ к значениям

```python
config.get('postgresql.host')          # '192.168.0.194'
config.get('binance.api_key')          # (из .env)
config.get('openrouter.model')         # Из YAML
config.telegram.get('bot_token', '')   # Из .env
```

### Релоад

```python
config.reload()  # Сброс singleton и перезагрузка
```

## 5. Внешние сервисы (network map)

```
┌──────────────────────────────────┐
│          CryptoTrader            │
├──────────────────────────────────┤
│ PostgreSQL: 192.168.0.194:5432   │
│ Ollama:      192.168.0.94:11434  │
│ AnythingLLM: 192.168.0.133:3001  │
│ RAGFlow:     (из env)            │
├──────────────────────────────────┤
│ Binance:  api.binance.com        │
│           testnet.binance.vision │
│ Bybit:    api.bybit.com          │
│           api-testnet.bybit.com  │
│ Bitfinex: (через config)         │
│ CoinEx:   (через config)         │
├──────────────────────────────────┤
│ OpenRouter API (LLM)             │
│ CryptoRank API (новости)         │
│ CoinDesk API (новости)           │
│ Telegram Bot API (уведомления)   │
│ Email SMTP (уведомления)         │
└──────────────────────────────────┘
```

## 6. Исключения

```python
class BybitAPIError(Exception):
    def __init__(self, ret_code: int, ret_msg: str, **kwargs):
        # ret_code: код ошибки Bybit
        # ret_msg: сообщение

class BinanceAPIError(Exception):
    def __init__(self, code: int, msg: str):
        # code: код ошибки Binance
        # msg: сообщение
```
