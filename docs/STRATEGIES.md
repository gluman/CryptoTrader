# CryptoTrader — Trading Strategies & Agents

## 1. Обзор стратегий

Система использует два типа стратегий:
1. **Rule-based** (правила + технические индикаторы) — Scalping, Intraday, Position
2. **LLM-based** (через TradingDecisionAgent с OpenRouter/Ollama)

Все rule-based стратегии наследуются от `BaseStrategy` (abc).

## 2. BaseStrategy — базовый класс

```python
# cryptotrader_strategies/base_strategy.py
from abc import ABC, abstractmethod

class BaseStrategy(ABC):
    SCALPING = 'scalping'
    INTRADAY = 'intraday'
    POSITION = 'position'

    def __init__(self, name: str, strategy_type: str):
        self.name = name
        self.strategy_type = strategy_type

    @abstractmethod
    def analyze(self, df_1m, df_5m, df_15m, df_1h, df_4h, df_1d, indicators) -> dict:
        """Возвращает сигнал: {action, confidence, entry_price, stop_loss, take_profit}"""
        pass

    @abstractmethod
    def get_timeframes(self) -> List[str]: ...
    @abstractmethod
    def get_default_symbols(self) -> List[str]: ...
    @abstractmethod
    def get_parameters(self) -> Dict[str, Any]: ...
```

## 3. ScalpingStrategy — 1m/5m

```python
# cryptotrader_strategies/scalping_strategy.py
class ScalpingStrategy(BaseStrategy):
```

**Философия**: быстрые входы-выходы, малые цели, жесткие стопы.

**Параметры**:
| Параметр | Значение | Описание |
|----------|----------|----------|
| max_loss_per_trade | 0.002 (0.2%) | Макс. убыток на сделку |
| target_profit | 0.003 (0.3%) | Цель по прибыли |
| max_position_size | 0.1 (10%) | Размер позиции от капитала |
| rsi_fast | 5 | Период RSI для скальпинга |
| volume_spike | 1.5x | Порог всплеска объема |
| min_confidence | 0.35 | Мин. уверенность для входа |

**Технические индикаторы**:
- Fast RSI (5) — oversold < 35, overbought > 65
- Bollinger Bands (10, 2 std) — касание границ + объем
- EMA cross (5/15) — быстрый/медленный пересечение
- Volume ratio (>1.5x от SMA20)

**Сигналы BUY**:
- RSI oversold bounce (RSI < 35 → рост)
- Нижняя граница BB + всплеск объема
- EMA 5 crosses above EMA 15

**Сигналы SELL**:
- RSI overbought reversal (RSI > 65 → падение)
- Верхняя граница BB + всплеск объема
- EMA 5 crosses below EMA 15

```python
# Пример результата
{
    'action': 'BUY',
    'confidence': 0.75,
    'entry_price': 65432.10,
    'stop_loss': 65301.23,     # -0.2%
    'take_profit': 65628.40,   # +0.3%
    'strategy': 'Scalping',
    'timeframes': ['1m', '5m'],
    'reasoning': 'Scalping BUY: RSI=32.5, BB_pos=0.12, Vol_ratio=2.1x, EMA_cross=True'
}
```

## 4. IntradayStrategy — 15m/1h/4h

```python
# cryptotrader_strategies/intraday_strategy.py
class IntradayStrategy(BaseStrategy):
```

**Философия**: внутридневные сделки на несколько часов.

**Параметры**:
| Параметр | Значение |
|----------|----------|
| max_loss_per_trade | 0.015 (1.5%) |
| target_profit | 0.025 (2.5%) |
| max_position_size | 0.2 (20%) |
| rsi_period | 14 |
| rsi_oversold/overbought | 40/60 |
| volume_spike | 1.3x |
| min_confidence | 0.60 |

**Технические индикаторы**:
- RSI (14) — oversold < 40, overbought > 60
- MACD (12/26/9) — пересечение сигнальной линии
- EMA (20/50) — тренд
- 4h подтверждение — для фильтрации ложных сигналов

**Сигналы BUY**:
- RSI oversold bounce (<40 → рост)
- MACD cross up + RSI < 50
- RSI < 45 + EMA 20 > EMA 50 (тренд вверх)

**Сигналы SELL**:
- RSI overbought reversal (>60 → падение)
- MACD cross down + RSI > 50
- RSI > 55 + EMA bearish

```python
# Пример
{
    'action': 'BUY',
    'confidence': 0.72,
    'stop_loss': round(price * 0.985, 8),  # -1.5%
    'take_profit': round(price * 1.025, 8), # +2.5%
    'timeframes': ['1h', '4h'],
    'reasoning': 'Intraday BUY: RSI=38.2, MACD_hist=12.45, EMA_trend=UP, Vol=1.5x, 4h_confirm=YES'
}
```

## 5. PositionStrategy — 4h/1d/1w

```python
# cryptotrader_strategies/position_strategy.py
class PositionStrategy(BaseStrategy):
```

**Философия**: долгосрочные свинг-сделки, удержание дней/недель.

**Параметры**:
| Параметр | Значение |
|----------|----------|
| max_loss_per_trade | 0.05 (5%) |
| target_profit | 0.12 (12%) |
| max_position_size | 0.3 (30%) |
| rsi_strong_oversold | 30 |
| rsi_strong_overbought | 70 |
| volume_spike | 1.2x |
| min_confidence | 0.55 |

**Технические индикаторы**:
- RSI (14) — oversold < 35, strong < 30
- SMA (20/50/200) — глобальный тренд
- MACD (12/26/9) — на daily
- 4h подтверждение тренда

**Сигналы BUY**:
- RSI strong oversold (<30) или RSI < 35 + strong_uptrend
- MACD cross up + RSI < 50
- RSI < 40 + price > SMA20 > SMA50

**Сигналы SELL**:
- RSI strong overbought (>70) или RSI > 65 + strong_downtrend
- MACD cross down + RSI > 50
- RSI > 60 + price < SMA20 < SMA50

```python
{
    'action': 'SELL',
    'confidence': 0.78,
    'stop_loss': round(price * 1.05, 8),    # +5%
    'take_profit': round(price * 0.88, 8),  # -12%
    'timeframes': ['1d', '4h'],
    'reasoning': 'Position SELL: RSI=72.5, MACD_hist=-15.2, Trend=STRONG_DOWN, Vol=1.8x, 4h_confirm=YES'
}
```

## 6. Сравнение стратегий

| Характеристика | Scalping | Intraday | Position |
|----------------|----------|----------|----------|
| Таймфреймы | 1m, 5m | 15m, 1h, 4h | 4h, 1d, 1w |
| Max loss | 0.2% | 1.5% | 5% |
| Target profit | 0.3% | 2.5% | 12% |
| Max позиция | 10% капитала | 20% | 30% |
| Min confidence | 35% | 60% | 55% |
| Индикаторы | Fast RSI, BB, EMA | RSI, MACD, EMA | RSI, SMA, MACD |
| Скорость | Секунды-минуты | Часы | Дни |

## 7. Агенты стратегий (исполнители)

### ScalpingAgent
```python
# cryptotrader_strategies/agents/scalping_agent.py
class ScalpingAgent:
    def __init__(self):
        self.strategy = ScalpingStrategy()
        self.symbols = ['BTCUSDT', 'ETHUSDT']

    def run(self):
        for symbol in self.symbols:
            df_1m = self.get_ohlcv(symbol, '1m', limit=50)
            df_5m = self.get_ohlcv(symbol, '5m', limit=100)
            signal = self.strategy.analyze(df_1m=df_1m, df_5m=df_5m, ...)
            if signal['action'] != 'HOLD':
                self._save_signal(symbol, signal)  # → strategy_signals + signals
```

### IntradayAgent
```python
class IntradayAgent:
    def __init__(self):
        self.strategy = IntradayStrategy()
        self.symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']

    def run(self):
        for symbol in self.symbols:
            df_15m = self.get_ohlcv(symbol, '15m', limit=200)
            df_1h = self.get_ohlcv(symbol, '1h', limit=200)
            df_4h = self.get_ohlcv(symbol, '4h', limit=200)
            signal = self.strategy.analyze(df_15m=df_15m, df_1h=df_1h, df_4h=df_4h, ...)
```

### PositionAgent
```python
class PositionAgent:
    def __init__(self):
        self.strategy = PositionStrategy()
        self.symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']

    def run(self):
        for symbol in self.symbols:
            df_4h = self.get_ohlcv(symbol, '4h', limit=200)
            signal = self.strategy.analyze(df_4h=df_4h, df_1d=df_4h, ...)
```

## 8. Сохранение сигналов

Все агенты сохраняют сигналы в **две таблицы**:
1. `strategy_signals` — детальные записи с SL/TP
2. `signals` (legacy) — для ExecutionAgent

```python
def _save_signal(self, symbol: str, signal: dict):
    # 1. StrategySignal (детальный)
    s = StrategySignal(
        symbol=symbol, exchange='bybit',
        strategy=self.strategy.name,
        action=signal['action'],
        confidence=..., entry_price=...,
        stop_loss=..., take_profit=...,
        timeframes=','.join(signal.get('timeframes', [])),
        reasoning=signal.get('reasoning', ''),
        status='pending'
    )
    # 2. Signal (legacy для ExecutionAgent)
    sig = Signal(
        symbol=symbol, exchange='bybit',
        market_type='linear',
        signal_type=signal['action'],
        confidence=..., reasoning=...,
        status='PENDING'
    )
```

## 9. LLM-based TradingDecisionAgent

Помимо rule-based стратегий, система использует LLM (через OpenRouter или Ollama)
для торговых решений. `TradingDecisionAgent`:

```python
class TradingDecisionAgent(BaseAgent):
    def run_once(self):
        # 1. Получает OHLCV + индикаторы из ohlcv_processed
        # 2. Получает сентимент из SentimentAgent
        # 3. Получает контекст из RAGFlow
        # 4. Формирует промпт с market_data + sentiment
        # 5. LLM вызов (OpenRouter/Ollama)
        # 6. Парсит ответ → сигнал BUY/SELL/HOLD
        # 7. Сохраняет в signals + decisions
```
