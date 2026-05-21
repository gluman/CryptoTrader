# CryptoTrader — Анализ и рекомендации по улучшению
# Дата: 2026-05-12
# Автор анализа: Claude Sonnet 4.6 (внешний агент)
# Файл создан для последующей обработки агентом-исполнителем

---

## КРАТКОЕ РЕЗЮМЕ

Система рабочая, архитектура продуманная. Выявлено:
- 4 критических бага (ломают функциональность прямо сейчас)
- 6 серьёзных архитектурных проблем
- 8 улучшений для снижения стоимости и повышения качества решений

---

## КРИТИЧЕСКИЕ БАГИ (исправить немедленно)

### BUG-01: Sentiment не использует LLM — только ключевые слова
**Файл:** `cryptotrader/src/agents/sentiment_agent.py`, метод `run_once()` → `analyze_sentiment()`

**Проблема:**
Метод `analyze_sentiment()` использует подсчёт ключевых слов (score += 0.2 за каждое слово).
Методы `call_llm()` и `call_llm_ollama()` определены, но НИКОГДА не вызываются в `run_once()`.
Документация в ALGORITHM.md обещает LLM-анализ — это ложь.

**Последствие:** Все сентимент-оценки примитивны. Заголовок "Bitcoin risk falls" 
получит +0.2 (за "falls") − 0.2 (за "risk") = 0.0 — неверный результат.

**Исправление:**
```python
# В методе run_once(), заменить строку:
score = self.analyze_sentiment(item["title"], item["summary"])
# На:
prompt = f"""Rate the crypto market sentiment from -1.0 (very bearish) to +1.0 (very bullish).
News title: {item["title"]}
Summary: {item["summary"][:300]}
Respond with ONLY a single decimal number between -1.0 and 1.0."""

raw = self.call_llm_ollama(prompt)  # Используем Ollama — бесплатно
try:
    score = float(raw.strip())
    score = max(-1.0, min(1.0, score))
except (ValueError, TypeError):
    score = self.analyze_sentiment(item["title"], item["summary"])  # fallback
```

**Важно:** Для sentiment достаточно Ollama (100 токенов на новость) — БЕСПЛАТНО.

---

### BUG-02: Скальпинг всегда даёт HOLD — конфликт SL/TP
**Файлы:**
- `cryptotrader/strategies/scalping_strategy.py`: SL = 0.2% (20 пунктов)
- `cryptotrader/src/agents/trading_agent.py`: `_validate_sl_tp()` требует SL >= 1.5%

**Проблема:**
`ScalpingStrategy.analyze()` генерирует сигнал с:
```
stop_loss = price * (1 - 0.002)  # 0.2% SL
take_profit = price * (1 + 0.003)  # 0.3% TP
```
Но `TradingDecisionAgent._validate_sl_tp()` проверяет:
```
min_sl_pct = 0.015  # 1.5% минимум
```
Результат: все скальпинг-сигналы принудительно меняются на HOLD.
Скальпинг-стратегия де-факто мертва.

**Исправление:** Добавить параметр `market_type` в `_validate_sl_tp()`:
```python
def _validate_sl_tp(self, signal, entry_price, sl, tp, atr, market_type="spot"):
    if market_type == "linear":  # скальпинг на фьючерсах
        min_sl_pct = 0.003   # 0.3% для скальпинга
        min_tp_pct = 0.006   # 0.6% для скальпинга
    else:
        min_sl_pct = 0.015   # 1.5% для spot
        min_tp_pct = 0.030   # 3.0% для spot
```

---

### BUG-03: Мёртвый код после return в _call_ollama()
**Файл:** `cryptotrader/src/agents/trading_agent.py`, строки 619-627

```python
        self.log("error", "All Ollama models failed")
        return {                        # ← ПЕРВЫЙ return
            "signal": "HOLD",
            "confidence": 0.0,
            "reasoning": "All Ollama models failed"
        }
        
        self.log("error", "All Ollama models failed")   # ← МЁРТВЫЙ КОД (строки 622-627)
        return {
            "signal": "HOLD",
            ...
        }
```
Строки 622-627 никогда не выполнятся. Удалить.

---

### BUG-04: _call_ollama() определён но не используется в call_llm()
**Файл:** `cryptotrader/src/agents/trading_agent.py`, метод `call_llm()`

```python
def call_llm(self, prompt):
    try:
        decision = self._call_deepseek(prompt)  # шаг 1
        return decision
    except Exception as e:
        return self._rule_based_decision(prompt)  # шаг 2: СРАЗУ правила!
```

Ollama (локальный сервер 192.168.0.94) полностью пропущен — это дешёвый fallback,
который должен быть между DeepSeek и rule-based.

**Исправление:**
```python
def call_llm(self, prompt):
    # 1. DeepSeek (платный, лучшее качество)
    try:
        return self._call_deepseek(prompt)
    except Exception as e:
        self.log("warning", f"DeepSeek failed: {e}")
    
    # 2. Ollama локальный (бесплатно)
    try:
        return self._call_ollama(prompt)
    except Exception as e:
        self.log("warning", f"Ollama failed: {e}")
    
    # 3. Rule-based (последний резерв)
    return self._rule_based_decision(prompt)
```

---

## СЕРЬЁЗНЫЕ АРХИТЕКТУРНЫЕ ПРОБЛЕМЫ

### ARCH-01: Две параллельные системы принятия решений не связаны

**Проблема:**
Существуют два независимых потока:
1. `TradingDecisionAgent` → LLM → таблица `signals`
2. `ScalpingAgent` → `ScalpingStrategy` → таблица `strategy_signals` + дублирует в `signals`

Они не знают о решениях друг друга. Возможен конфликт: LLM говорит SELL, скальпинг 
генерирует BUY в тот же момент для одного символа.

**Рекомендация:** Ввести `SignalOrchestrator`:
```python
class SignalOrchestrator:
    """Объединяет сигналы от всех источников и принимает финальное решение"""
    
    def decide(self, symbol: str) -> Dict:
        llm_signal = self.trading_agent.run_once_for_symbol(symbol)
        scalp_signal = self.scalping_agent.analyze(symbol)
        
        # Консенсус: если противоречат — HOLD
        if llm_signal["signal"] != scalp_signal["action"]:
            if llm_signal["signal"] != "HOLD" and scalp_signal["action"] != "HOLD":
                return {"signal": "HOLD", "reasoning": "Conflicting strategies"}
        
        # Приоритет: более уверенный сигнал
        if llm_signal["confidence"] >= scalp_signal["confidence"]:
            return llm_signal
        return scalp_signal
```

---

### ARCH-02: FDI реализован некорректно
**Файл:** `cryptotrader/src/agents/trading_agent.py`, метод `_calculate_fdi()`

**Проблема:**
Текущая реализация:
```python
log_range = np.log(np.abs(close - close.shift(1)) + 1e-10)
fd = sum_log / np.log(period)
fdi = 2 - fd
```
Это НЕ стандартный Fractal Dimension Index. Настоящий FDI (Sevcik/Hurst):
```
FDI = log(N) / (log(N) + log(N / (N-1) + log(Hmax/Hmin)))
```
где Hmax, Hmin — максимальный и минимальный high/low за период N.

Текущая реализация даёт ненадёжные значения режима рынка.

**Исправление (стандартный FDI):**
```python
def _calculate_fdi(self, close: pd.Series, high: pd.Series, 
                   low: pd.Series, period: int = 30) -> Dict:
    if len(close) < period:
        return {"fdi": 0.5, "regime": "unknown"}
    
    results = []
    for i in range(period, len(close)):
        h_max = high.iloc[i-period:i].max()
        h_min = low.iloc[i-period:i].min()
        if h_max <= h_min:
            results.append(0.5)
            continue
        log_n = np.log(period)
        log_ratio = np.log(h_max / h_min) if h_min > 0 else 0
        fdi = 1 + (log_n + np.log(period / (period - 1))) / (log_n + log_ratio + 1e-10)
        fdi = max(1.0, min(2.0, fdi))
        results.append((fdi - 1.0))  # нормализуем к 0..1
    
    fdi_val = results[-1] if results else 0.5
    regime = "trending" if fdi_val < 0.5 else "ranging"
    return {"fdi": round(fdi_val, 4), "regime": regime}
```

---

### ARCH-03: CSS не нормализован — порог 0.20 ненадёжен
**Файл:** `cryptotrader/src/agents/trading_agent.py`, метод `_calculate_css()`

**Проблема:**
CSS = `(MA - MA.shift(1)) / ATR` — значение не ограничено. 
При BTCUSDT: MA меняется на $500, ATR = $800 → CSS = 0.625
При PEPEUSDT: MA меняется на 0.000001, ATR = 0.000002 → CSS = 0.5
Оба дают > 0.20, но ситуации разные.

**Рекомендация:** Нормализовать к перцентилям или z-score за скользящее окно:
```python
# После расчёта slope:
slope_norm = slope / (slope.rolling(100).std() + 1e-10)  # z-score нормализация
css_current = float(slope_norm.iloc[-1].clip(-3, 3) / 3)  # → [-1, +1]
```
При z-score нормализации порог ±0.20 будет означать примерно 1σ отклонение.

---

### ARCH-04: Нет дедупликации сигналов — спам одинаковых PENDING
**Файл:** `cryptotrader/src/agents/trading_agent.py`, метод `save_decision()`

**Проблема:**
Каждые 15 минут для каждого символа создаётся новая запись в `signals`.
Если ExecutionAgent не успел обработать → накапливаются десятки PENDING BUY для BTCUSDT.

**Исправление:** Перед `save_decision` проверить существующие pending:
```python
def save_decision(self, symbol, exchange, timeframe, indicators, sentiment, decision, rag_context=""):
    # Пропустить HOLD — не сохранять в БД, только логировать
    if decision["signal"] == "HOLD":
        return None
    
    # Проверить: нет ли уже PENDING для этого символа
    with self.db.get_session() as session:
        existing = session.query(Signal).filter(
            Signal.symbol == symbol,
            Signal.status == "PENDING",
            Signal.signal_type == decision["signal"],
        ).count()
        if existing > 0:
            self.log("info", f"Skipping duplicate PENDING {decision["signal"]} for {symbol}")
            return None
    # ... дальше сохраняем
```

---

### ARCH-05: Нет мониторинга позиций в реальном времени (trailing stop не работает)
**Файл:** `cryptotrader/src/agents/execution_agent.py`

**Проблема:**
`update_position_prices()` и `check_stop_loss_take_profit()` определены, но
не вызываются в планировщике `scheduler.py`. 
Trailing stop "активируется" в БД, но цена не проверяется → SL/TP никогда не срабатывают!

**Исправление:** Добавить в `CryptoTraderScheduler.run_tick()`:
```python
# Каждые 60 секунд проверять позиции
if now - self.last_run["position_check"] >= 60:
    positions = executor.get_all_open_positions()
    for pos in positions:
        current_price = self.get_current_price(pos["symbol"], pos["exchange"])
        executor.update_position_prices(pos["symbol"], current_price)
        triggers = executor.check_stop_loss_take_profit(pos["symbol"], current_price)
        for t in triggers:
            executor.execute_stop_loss_take_profit(t)
    self.last_run["position_check"] = now
```

---

### ARCH-06: hardcoded model_version = "openrouter_v1" при использовании DeepSeek
**Файл:** `cryptotrader/src/agents/trading_agent.py`, метод `save_decision()`, строка 651

```python
model_version="openrouter_v1",   # ← неверно, используется DeepSeek
```
Исправить на: `model_version=decision.get("source", self.model)`

---

## ОПТИМИЗАЦИЯ СТОИМОСТИ LLM

### LLM-01: Батчинг — обрабатывать 3-4 символа в одном запросе
**Экономия:** уменьшение числа API-вызовов в 3-4 раза

**Проблема:** 10 символов → 10 отдельных вызовов DeepSeek = дорого.

**Рекомендация:** Новый метод `call_llm_batch()`:
```python
def call_llm_batch(self, decisions_data: list) -> list:
    """Анализ 3-4 символов в одном LLM-запросе"""
    combined = ""
    for i, d in enumerate(decisions_data, 1):
        combined += f"### Symbol {i}: {d["symbol"]}\n"
        combined += self._build_compact_prompt(d["symbol"], d["indicators"], d["sentiment"])
        combined += "\n---\n"
    
    prompt = f"""Analyze {len(decisions_data)} crypto trading opportunities.
For EACH symbol respond with JSON on a separate line.

{combined}

Respond with {len(decisions_data)} JSON lines, one per symbol:
{{"symbol": "X", "signal": "BUY/SELL/HOLD", "confidence": 0.0-1.0, "reasoning": "brief"}}"""
    
    # Один вызов API вместо N вызовов
    response = self._call_deepseek(prompt)
    return self._parse_batch_response(response, decisions_data)
```

---

### LLM-02: Использовать Ollama как основной для сентимента
**Экономия:** ~$0/месяц вместо платного API для sentiment

**Сервер Ollama уже доступен:** `http://192.168.0.94:11434`
Для sentiment нужно только 100 токенов на новость → идеально для qwen3.5:9b (бесплатно).

**Конкретный шаг:** В `sentiment_agent.py` `run_once()` вызывать `call_llm_ollama()` 
вместо `analyze_sentiment()` (см. BUG-01 выше).

---

### LLM-03: Pre-filter — вызывать LLM только при неопределённости правил
**Экономия:** снижение LLM-вызовов на 40-60%

**Логика:** Если правила дают чёткий сигнал (CSS > 0.35, RSI < 55, MACD > 0),
LLM только добавит шум. LLM нужен только в зоне неопределённости.

```python
def should_call_llm(self, indicators: dict) -> bool:
    """Нужен ли LLM для этого решения?"""
    css = abs(indicators.get("css_value", 0))
    rsi = indicators.get("rsi_14", 50)
    
    # Чёткий сигнал — правила надёжнее
    if css > 0.35 and (rsi < 40 or rsi > 60):
        return False
    
    # Неопределённость — нужен LLM
    return True
```

---

### LLM-04: Компактный промт — убрать лишние поля
**Экономия:** 30-40% токенов на запрос

**Текущий промт:** ~800 токенов (полный)
**Сокращённый вариант:** ~400 токенов

Убрать из промта:
- Примеры SL/TP (дублирует правила)
- Детальное описание FDI/режима (уместить в 1 строку)
- RAG context > 500 символов

```python
def build_compact_prompt(self, symbol, indicators, sentiment):
    regime = indicators.get("regime", "unknown")
    return f"""Crypto signal for {symbol}:
Price={indicators["price"]:.4f} RSI={indicators["rsi_14"]:.1f} CSS={indicators["css_value"]:.4f}
MACD_hist={indicators["macd_hist"]:.6f} BB=[{indicators["bb_lower"]:.4f}-{indicators["bb_upper"]:.4f}]
Volume_ratio={indicators["volume_ratio"]:.1f}x Sentiment={sentiment["avg_sentiment"]:.2f} Regime={regime}
CSS_cross_up={indicators["css_cross_up"]} CSS_cross_down={indicators["css_cross_down"]}
Rules: BUY if CSS>0.2+cross+RSI<70+sentiment>0. SELL if CSS<-0.2+RSI>30. HOLD otherwise.
SL: 1.5-3% from entry. TP: min 3%, RR>=1.5. JSON only: {{"signal":"BUY/SELL/HOLD","confidence":0.0,"reasoning":"brief","stop_loss":null,"take_profit":null}}"""
```

---

### LLM-05: Кэширование sentiment — не пересчитывать если новостей нет
**Экономия:** 50-70% LLM-вызовов для sentiment

```python
def run_once(self):
    news = self.get_unanalyzed_news()
    if not news:
        self.log("info", "No new news to analyze — skipping LLM calls")
        return {"analyzed": 0, "aggregated": self.get_aggregated_sentiment()}
    # ... анализируем только новое
```

---

## УЛУЧШЕНИЯ СИСТЕМЫ ПРИНЯТИЯ РЕШЕНИЙ

### DECISION-01: Добавить многотаймфреймовую фильтрацию
**Проблема:** Решение принимается только по 1h (или 1m для скальпинга).
Ложные сигналы на малых ТФ не фильтруются старшим трендом.

**Рекомендация:** Добавить проверку 4h тренда как фильтр:
```python
def get_higher_tf_bias(self, symbol: str, exchange: str = "binance") -> str:
    """Определить тренд на 4h для фильтрации сигналов"""
    df_4h = self.get_ohlcv_data(symbol, exchange, "4h", limit=50)
    if df_4h.empty:
        return "NEUTRAL"
    
    close = df_4h["close"]
    sma20_4h = close.rolling(20).mean().iloc[-1]
    
    if close.iloc[-1] > sma20_4h * 1.005:
        return "BULLISH"
    elif close.iloc[-1] < sma20_4h * 0.995:
        return "BEARISH"
    return "NEUTRAL"

# В run_once_for_symbol():
bias_4h = self.get_higher_tf_bias(symbol, exchange)
# Добавить в промт: f"4h trend bias: {bias_4h}"
# Правило: не открывать BUY если bias_4h == "BEARISH"
```

---

### DECISION-02: Circuit breaker — стоп при серии потерь
**Проблема:** Нет защиты от серии убыточных сделок подряд.

```python
def check_circuit_breaker(self) -> bool:
    """True если нужно остановить торговлю"""
    with self.db.get_session() as session:
        from datetime import timedelta
        since = datetime.utcnow() - timedelta(hours=24)
        recent_trades = session.query(Trade).filter(
            Trade.created_at >= since
        ).order_by(Trade.created_at.desc()).limit(5).all()
        
        if len(recent_trades) < 3:
            return False
        
        losing = sum(1 for t in recent_trades[:5] 
                    if t.pnl_percent and float(t.pnl_percent) < -1.0)
        if losing >= 3:
            self.log("warning", "Circuit breaker: 3+ losses in last 5 trades")
            return True
    return False
```

---

### DECISION-03: Адаптивный min_confidence по волатильности
**Проблема:** `min_confidence = 0.35` — фиксированный порог вне зависимости от рынка.
В высоковолатильных условиях нужна более высокая уверенность.

```python
def get_adaptive_min_confidence(self, atr: float, price: float) -> float:
    """ATR/price ratio > 3% = высокая волатильность → выше порог"""
    atr_pct = (atr / price) * 100 if price > 0 else 0
    
    if atr_pct > 5.0:
        return 0.75  # очень волатильно
    elif atr_pct > 3.0:
        return 0.60
    elif atr_pct > 1.5:
        return 0.50
    return self.min_confidence  # базовый
```

---

## ИТОГОВЫЙ ПРИОРИТЕТНЫЙ ПЛАН

| Приоритет | ID | Описание | Трудозатраты |
|-----------|-----|----------|-------------|
| 🔴 КРИТИЧНО | BUG-01 | Включить LLM для сентимента (Ollama) | 30 мин |
| 🔴 КРИТИЧНО | BUG-02 | Исправить SL порог для скальпинга | 15 мин |
| 🔴 КРИТИЧНО | BUG-04 | Вставить Ollama в цепочку fallback | 10 мин |
| 🟠 ВАЖНО | ARCH-05 | Подключить мониторинг позиций (trailing stop) | 2 часа |
| 🟠 ВАЖНО | ARCH-01 | SignalOrchestrator — объединить стратегии | 4 часа |
| 🟠 ВАЖНО | ARCH-04 | Дедупликация сигналов | 30 мин |
| 🟡 СРЕДНЕ | LLM-01 | Батчинг символов (3-4 в одном запросе) | 2 часа |
| 🟡 СРЕДНЕ | LLM-03 | Pre-filter для LLM-вызовов | 1 час |
| 🟡 СРЕДНЕ | DECISION-01 | Мультитаймфреймовая фильтрация | 2 часа |
| 🟡 СРЕДНЕ | ARCH-02 | Исправить FDI формулу | 1 час |
| 🟢 НИЗКО | LLM-02 | Компактный промт | 1 час |
| 🟢 НИЗКО | DECISION-02 | Circuit breaker | 1 час |
| 🟢 НИЗКО | ARCH-03 | CSS z-score нормализация | 1 час |
| 🟢 НИЗКО | BUG-03 | Удалить мёртвый код | 5 мин |

---

## МОДЕЛИ LLM — РЕКОМЕНДАЦИИ ПО СТОИМОСТИ

Текущая конфигурация:
- **DeepSeek-chat** (api.deepseek.com) — основной: $0.14/1M input, $0.28/1M output ✅ дёшево
- **Ollama** (192.168.0.94) — qwen3.5:9b или minimax-m2.7 — БЕСПЛАТНО, но не используется

Рекомендуемая схема по приоритету стоимости:
1. **Sentiment** → Ollama (бесплатно, 100 токенов, qwen3.5:9b отлично)
2. **Trading decisions** → DeepSeek-chat V3 (дёшево, хорошее качество)
3. **Fallback trading** → Ollama (бесплатно)
4. **Last resort** → Rule-based (бесплатно, без LLM)

При батчинге 3 символов на запрос и pre-filter оценочная экономия: ~60% текущих LLM-расходов.

---

*Анализ выполнен: 2026-05-12*
*Следующий агент-исполнитель: применить исправления согласно приоритетам*
*Начать с: BUG-01, BUG-02, BUG-04 (суммарно ~1 час работы)*
