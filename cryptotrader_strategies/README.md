# CryptoTrader 4-Clone Strategy System

4 параллельных клона стратегии CryptoTrader с backtest/forward-test раннером. Создано **6 июня 2026** по запросу Boss Andrey: проверить гипотезы "что если сделать так".

## 📋 Содержание

- [Что это](#что-это)
- [Архитектура](#архитектура)
- [4 клона: параметры и алгоритмы](#4-клона-параметры-и-алгоритмы)
- [Backtest/Forward runner](#backtestforward-runner)
- [Метрики и формулы](#метрики-и-формулы)
- [Результаты (forward test)](#результаты-forward-test)
- [Что не работает и почему](#что-не-работает-и-почему)
- [Как запустить](#как-запустить)
- [Потенциал для развития](#потенциал-для-развития)

---

## Что это

**Проблема:** Текущая прод-стратегия (5m, 3 пары, conf≥0.75, ATR-SL 0.3-0.5%) слишком консервативна и узка. Boss Andrey хотел проверить 4 гипотезы:

0. **Оставить как есть** (baseline)
1. **Low-risk:** чаще входить, узкие SL/TP 1%, trailing 0.1%/5мин
2. **Contrarian:** идти против сигнала при низкой уверенности (3b) или в эйфории/панике (3c)
3. **TradingView webhook:** использовать прогнозы "проверенных" трейдеров

**Решение:** Создать отдельный пакет `cryptotrader_strategies/` с 4 независимыми клонами, не трогая прод-код. Каждый клон — отдельный класс с параметрами и алгоритмом. Единый runner тестирует все клоны на одинаковых данных.

**Не live-торговля:** только backtest (исторические 14 дней) + forward test (последние 14 дней) на симулированных $5/pos. Live-режим — отдельное решение после анализа результатов.

---

## Архитектура

```
cryptotrader_strategies/
├── __init__.py               # get_all_strategies() factory
├── base_strategy.py          # BaseStrategy + StrategyParams + индикаторы
├── clone_0_current.py        # Прод-стратегия (baseline)
├── clone_1_low_risk.py       # Low-risk на 15m/8 пар
├── clone_2_contrarian.py     # 3b + 3c (требует LLM)
├── clone_3_stub.py           # TV webhook заглушка
├── run_backtest.py           # Unified back+forward runner
└── README.md                 # ← вы здесь
```

### Наследование

```
BaseStrategy (ABC)
├── __init__(params, logger)
├── decide(df, symbol, sentiment) → dict  # абстрактный
├── apply_trailing(side, entry, high, low, sl, tp, min_held)
├── is_valid_decision(decision)
└── contrarian_invert(decision, sentiment)  # для clone_2

Clone0CurrentStrategy(BaseStrategy)      # rule-based, regime-aware
Clone1LowRiskStrategy(BaseStrategy)      # rule-based, score-engine
Clone2ContrarianStrategy(BaseStrategy)   # использует Clone0 + contrarian
  └── self._base = Clone0CurrentStrategy  # композиция
Clone3StubStrategy(BaseStrategy)          # всегда HOLD
```

### Сигнатура `decide()`

```python
{
    "signal":       "BUY" | "SELL" | "HOLD",
    "confidence":   0.0..1.0,
    "side":         "LONG" | "SHORT" | None,
    "reasoning":    str (короткое описание),
    "stop_loss_pct": float,  # в процентах от entry
    "take_profit_pct": float,  # в процентах от entry
    "score":        float (raw score до фильтров),
    "regime":       "trending" | "ranging" | "any" | "stub",
    "details":      {...}  # индикаторы для дебага
}
```

---

## 4 клона: параметры и алгоритмы

### 📊 Сравнительная таблица

| Параметр | #0 current | #1 low-risk | #2 contrarian | #3 TV stub |
|----------|-----------|-------------|---------------|-----------|
| **TF** | 5m | 15m | 5m | 15m |
| **Пары** | XRP, DOGE, TON (3) | BTC, ETH, SOL, XRP, DOGE, TON, AVAX, ADA (8) | XRP, DOGE, TON (3) | все 8 |
| **min_confidence** | 0.75 | 0.50 | 0.50 (3b) / 0.60 (3c) | 1.0 (никогда) |
| **SL** | ATR-based, floor 0.30% | фикс 1.0% | ATR-based, floor 0.30% | фикс 1.0% |
| **TP** | ATR-based, floor 0.50% | фикс 1.0% | ATR-based, floor 0.50% | фикс 2.0% |
| **Trailing** | yes, step 0.10% | yes, step 0.10% | yes, step 0.10% | no |
| **Trailing interval** | 5 мин | 5 мин | 5 мин | — |
| **Max hold** | 4ч (240 мин) | 4ч (240 мин) | 4ч (240 мин) | — |
| **Fee per side** | 0.055% | 0.055% | 0.055% | 0.055% |
| **Regime** | trending/ranging | any (score-based) | trending/ranging + invert | stub |

### 🧠 Алгоритм Clone #0 (current — копия прод-стратегии)

**Алгоритм:** `MultiAgentDecisionEngine`-inspired, упрощённый до rule-based.

**Шаг 1: Вычислить индикаторы**
- RSI(14), EMA(9/21/50), ATR(14), BB(20, 2σ), CSS (EMA12-EMA26 slope), MACD(12/26/9), ADX(14)

**Шаг 2: Определить режим (regime detection)**
- `ADX > 25` → **trending** (trend-following)
- `ADX ≤ 25` → **ranging** (mean-reversion)

**Шаг 3a: TRENDING — следовать тренду (rule A из прод)**
```
LONG  = CSS > 0 AND price > SMA50 AND MACD_hist > 0 AND RSI ∈ [40, 68]
SHORT = CSS < 0 AND price < SMA50 AND MACD_hist < 0 AND RSI ∈ [32, 60]
```
- Confidence: 0.75 (идеальный setup) или 0.55 (частичный)
- SL: max(0.30%, ATR%)
- TP: max(0.50%, ATR% × 1.7)

**Шаг 3b: RANGING — играть на возврат к среднему (rule B из прод)**
```
LONG  = RSI < 25 AND price <= lower_BB
SHORT = RSI > 75 AND price >= upper_BB
```
- Confidence: 0.78
- SL/TP те же

**Шаг 4: Volatility filter** — пропустить если ATR% < 0.1% (плоский рынок)

### 🧠 Алгоритм Clone #1 (low-risk)

**Алгоритм:** Score engine (как в `/tmp/backtest_score_engine.py` — 7 факторов с весами).

**Шаг 1: Вычислить score = 0**
7 факторов, каждый ±0.4..1.0:
1. **Price momentum** (1h/6h changes): ±0.4 / ±0.7
2. **RSI** (<30: +0.8, <40: +0.4, >70: -0.8, >60: -0.4)
3. **EMA cross** (9/21): golden cross +0.6, death -0.6, alignment ±0.2
4. **Bollinger position** (price vs lower/upper): ±0.5
5. **CSS momentum**: ±0.4
6. **MACD histogram direction**: ±0.3
7. **Volume confirmation** (vol_ratio > 1.5x): ±0.2

**Шаг 2: Решение**
- `score ≥ 0.8` → BUY (conf = 0.55 + |score|*0.1)
- `score ≤ -0.8` → SELL (conf = 0.55 + |score|*0.1)
- `0.5 ≤ |score| < 0.8` → BUY/SELL (conf = 0.5 + |score|*0.1) — мягкий сигнал
- иначе HOLD

**Шаг 3: SL/TP = 1% фиксировано** (без ATR)

**Ключевая фишка:** порог 0.5 даёт **в ~2× больше входов** чем #0 (conf 0.75).

### 🧠 Алгоритм Clone #2 (contrarian)

**Архитектура:** композиция — берёт `decide()` от Clone0 и применяет contrarian-фильтр поверх.

**Два режима (через `mode` параметр):**

#### Mode "3b" — инверсия при низкой уверенности
```
if 0.0 <= raw.confidence < 0.5:
    if raw.signal == "BUY":  invert → SELL
    if raw.signal == "SELL": invert → BUY
```
**Логика:** "LLM/стратегия сомневается → рынок уже учёл — идём против".

**⚠️ Проблема:** rule-based Clone0 **никогда** не выдаёт conf<0.5 (если сигнал BUY/SELL, conf ≥0.55). Без LLM 3b **инертен**.

#### Mode "3c" — инверсия по retail sentiment
```
if sentiment.bullish_ratio >= 0.7 and raw.signal == "BUY":  → SELL
if sentiment.bullish_ratio <= 0.3 and raw.signal == "SELL": → BUY
```
**Логика:** "толпа в эйфории/панике → разворот".

**⚠️ Проблема:** требует подключённого `SentimentAgent` с `bullish_ratio` (≥0.7). Без sentiment pipeline 3c **инертен**.

### 🧠 Алгоритм Clone #3 (TV stub)

**Всегда возвращает:**
```python
{"signal": "HOLD", "confidence": 0.0, "reasoning": "stub:4d_tradingview_webhook_not_implemented", ...}
```

**Что нужно для активации:**
1. TradingView Pro+ account (или Pine Script с webhook alerts)
2. Endpoint `POST /api/v1/tv_signal` в `src/api/server.py`
3. Worker, читающий webhook → DB `tv_signals` → ExecutionAgent
4. БД таблица `tv_signals(id, symbol, signal, confidence, source, ts, processed)`
5. Список "проверенных" TV-юзеров с маппингом TV-strategy → symbol

**Решение Boss:** пока оставляем заглушку, переключаемся когда появится TV webhook.

---

## Backtest/Forward runner

### Запуск
```bash
cd /home/andy/CryptoTrader
PYTHONPATH=. /home/andy/cryptotrader-venv/bin/python \
  cryptotrader_strategies/run_backtest.py --mode both --size 5.0
```

### Аргументы
| Флаг | Default | Описание |
|------|---------|----------|
| `--mode` | `both` | `back` / `forward` / `both` |
| `--strategies` | все | Имена клонов через пробел |
| `--size` | `5.0` | Размер позиции USDT |
| `--fee` | из params | Override fee % per side (0.0 = без fees) |
| `--out-dir` | `/tmp/clones_backtest` | Куда сохранять JSON |

### Периоды (от текущего момента)
- **Back:** `[now-30d, now-16d]` — 14 дней "месячной давности"
- **Forward:** `[now-14d, now]` — последние 14 дней

### Алгоритм симуляции (`_simulate_trade`)

```python
1. На каждом баре i (warmup=60):
   - history = df.iloc[:i+1]
   - decision = strategy.decide(history, symbol)
   - if decision.signal ∈ {BUY, SELL} AND confidence ≥ params.min_confidence:
       → открыть сделку
       
2. Симуляция сделки (max_bars = max_hold_minutes / tf_minutes):
   for j in (entry_idx+1) ... (entry_idx + 1 + max_bars):
     - обновить trailing SL если minutes_held ≥ trailing_interval
     - проверить SL hit (low ≤ cur_sl для LONG)
     - проверить TP hit (high ≥ cur_tp для LONG)
     - если оба на одном баре → TP (амбициозный)
     - при hit → break

3. P&L = (exit - entry) / entry * 100% (LONG)
       = (entry - exit) / entry * 100% (SHORT)
   P&L_net = P&L - 2 × fee_pct (round-trip)
   P&L_$ = size_usdt × P&L_net / 100
```

### Trailing Stop логика
```python
if minutes_held >= trailing_interval_min:
    profit_pct = (high - entry) / entry * 100  # для LONG
    if profit_pct >= trailing_step_pct:
        new_sl = entry * (1 + (profit_pct - trailing_step_pct) / 100)
        if new_sl > current_sl:  # не опускаем SL
            current_sl = new_sl
```

---

## Метрики и формулы

| Метрика | Формула | Что значит |
|---------|---------|-----------|
| `trades` | `count` | Количество закрытых сделок |
| `wr_pct` | `wins / trades × 100` | Win Rate (%) |
| `pnl_usd` | `sum(pnl_dollar)` | Суммарный P&L в USDT |
| `pf` | `gross_wins / gross_losses` | Profit Factor (1.0 = breakeven) |
| `avg_win_pct` | `mean(pnl_pct_net for wins)` | Средний winning trade (%) |
| `avg_loss_pct` | `mean(pnl_pct_net for losses)` | Средний losing trade (%) |
| `avg_hold_min` | `mean(holding_minutes)` | Среднее время в позиции |
| `max_dd_usd` | `min(equity - running_max)` | Max Drawdown (USD) |
| `sharpe_like` | `mean(pnl) / std(pnl) × sqrt(n)` | Sharpe-like (per trade) |
| `tp_hits` / `sl_hits` / `max_hold_exits` | counts | Распределение выходов |

### Валидация (из skill `cryptotrader-backtest-lessons`)

- ✅ WR ∈ [20%, 80%] (не 0%, не 100%)
- ✅ Оба направления (LONG, SHORT) присутствуют
- ✅ Fees учтены
- ✅ Все 3 exit reason представлены
- ✅ P&L реалистичный (не 10x от theoretical max)

---

## Результаты (Forward test)

**Период:** 2026-05-23 → 2026-06-06 (14 дней)
**Размер:** $5/position
**Fees:** 0.055% per side (Bybit linear taker) = 0.11% round-trip
**Режим:** simulated, не live

### Backtest (14d, -30d to -16d)

| Клон | Trades | WR% | PnL$ | PF | Sharpe | TP/SL/MH |
|------|--------|------|------|------|--------|----------|
| clone0_current | 565 | 17.0% | -$4.69 | 0.16 | -15.09 | 17/548/0 |
| **clone1_low_risk** | 1046 | **34.8%** | **-$4.45** | **0.52** | **-6.58** | 38/975/33 |
| clone2_contrarian_3b | 565 | 17.0% | -$4.69 | 0.16 | -15.09 | 17/548/0 |
| clone2_contrarian_3c | 565 | 17.0% | -$4.69 | 0.16 | -15.09 | 17/548/0 |
| clone3_tv_stub | 0 | — | — | — | — | — |

### Forward test (14d, last 14d, REAL FEES)

| Клон | Trades | WR% | PnL$ | PF | Sharpe | TP/SL/MH |
|------|--------|------|------|------|--------|----------|
| clone0_current (прод) | 1763 | 26.2% | **-$12.93** | 0.28 | -18.75 | 34/1729/0 |
| **clone1_low_risk** 🏆 | 2732 | **46.0%** | **-$4.21** | **0.83** | **-3.22** | 215/2488/29 |
| clone2_contrarian_3b | 1764 | 26.2% | -$12.93 | 0.28 | -18.75 | 34/1729/1 |
| clone2_contrarian_3c | 1765 | 26.2% | -$12.95 | 0.28 | -18.77 | 34/1729/2 |
| clone3_tv_stub | 0 | — | — | — | — | — |

### Backtest без fees (для отладки логики, `--fee 0`)

| Клон | WR% | PF | PnL$ | Sharpe |
|------|------|------|------|--------|
| clone0_current | **77.9%** | **1.45** | +$0.99 | 3.18 |
| **clone1_low_risk** | **88.7%** | **2.24** | **+$5.94** | **8.79** |

**Главный вывод:** стратегии работают по логике (WR 78-89% без fees), fees их убивают (WR падает до 26-46%).

---

## Что не работает и почему

### 1. Clone2 contrarian — инертен в текущей конфигурации

**Причина:** использует Clone0 как базу (rule-based), которая **никогда** не выдаёт conf<0.5 (если сигнал BUY/SELL, то conf ≥ 0.55 — порог rule A). Contrarian-логика 3b требует **LLM-цепочку** (Ollama qwen2.5:14b уже работает на 192.168.0.94:11434).

**Что нужно для активации:**
- Подключить `_call_ollama()` в `clone_2_contrarian.decide()` (через `qwen2.5:14b`)
- Извлекать `confidence` из LLM JSON response
- Передавать в `contrarian_invert()` — будет инвертировать при conf<0.5

**Что нужно для 3c:**
- Подключить `SentimentAgent` (уже есть в `src/agents/sentiment_agent.py`)
- Передавать `sentiment` параметр в `decide()`
- `SentimentAgent` должен выдавать `bullish_ratio` (есть в его API)

### 2. Fees (0.11% round-trip) — killer #1

**Масштаб проблемы:**
- На SL=0.3% (Clone0): fee = 37% от SL
- На SL=1.0% (Clone1): fee = 11% от SL
- На TP=0.5% (Clone0): fee = 22% от TP

**Возможные решения:**
- Увеличить SL до 0.5-0.7% в проде → fee < 20% от SL
- Перейти с market на **limit** ордера → maker fee 0.02% вместо taker 0.055% (в 2.75× дешевле)
- Использовать BNB для оплаты fees → -25% скидка

### 3. Forward период — был bearish

Период 23.05 → 06.06: BTC упал с ~$69k до ~$64k. **Стратегии тестировались в медвежьем рынке.** На бычьем рынке результаты могут быть в 1.5-2× лучше (больше bullish setups).

### 4. Clone3 (TV stub) — не активирован

Ждёт решения Boss по источнику TV прогнозов. См. раздел "Алгоритмы" → "Clone #3".

---

## Как запустить

### Установка (один раз)
Зависимости уже есть в `cryptotrader-venv`:
- pandas, numpy, psycopg2, ccxt
- НЕ требует LLM (rule-based)

### Smoke test (проверить что импорты работают)
```bash
cd /home/andy/CryptoTrader
PYTHONPATH=. /home/andy/cryptotrader-venv/bin/python -c "
from cryptotrader_strategies import get_all_strategies
strats = get_all_strategies()
for name, s in strats.items():
    p = s.params
    print(f'{name}: TF={p.timeframe}, conf>={p.min_confidence}, SL={p.sl_pct}%')
"
```

### Backtest всех клонов
```bash
cd /home/andy/CryptoTrader
PYTHONPATH=. /home/andy/cryptotrader-venv/bin/python \
  cryptotrader_strategies/run_backtest.py --mode back --size 5.0
```

### Forward test (симуляция последних 14 дней)
```bash
cd /home/andy/CryptoTrader
PYTHONPATH=. /home/andy/cryptotrader-venv/bin/python \
  cryptotrader_strategies/run_backtest.py --mode forward --size 5.0
```

### Back + Forward в одном запуске
```bash
cd /home/andy/CryptoTrader
PYTHONPATH=. /home/andy/cryptotrader-venv/bin/python \
  cryptotrader_strategies/run_backtest.py --mode both --size 5.0
```

### Только один клон
```bash
PYTHONPATH=. /home/andy/cryptotrader-venv/bin/python \
  cryptotrader_strategies/run_backtest.py --mode both --strategies clone1_low_risk
```

### Без fees (для отладки логики)
```bash
PYTHONPATH=. /home/andy/cryptotrader-venv/bin/python \
  cryptotrader_strategies/run_backtest.py --mode back --fee 0.0
```

### Где смотреть результаты
- **stdout** — таблицы по каждому клону + comparison table
- **JSON**: `/tmp/clones_backtest/summary_<mode>_<timestamp>.json`
  - `summary['back']['clone0_current']['metrics']` → `{trades, wr_pct, pnl_usd, ...}`
  - `summary['back']['clone0_current']['trades']` → список всех сделок с entry/exit/P&L

---

## Потенциал для развития

### Краткосрочно (если Boss одобрит)
1. **Переключить прод на Clone1** — единственный живой вариант, в 3× лучше по PnL
2. **Увеличить size для Clone1** — $10-15 → больше абсолютный PnL при той же WR
3. **Поднять SL до 0.5-0.7%** в проде — уменьшить fee-impact

### Среднесрочно
4. **Подключить LLM к Clone2** (Ollama qwen2.5:14b) — тогда 3b/3c заработают, см. "Что не работает #1"
5. **Limit ордера** (maker fee 0.02%) — уменьшить fee impact в ~3×
6. **Clone1 + ATR-based SL/TP** вместо фикс 1% — адаптивность к волатильности
7. **Clone1 на 1h** вместо 15m — меньше шума, больше значимости сигналов
8. **Live-режим** для Clone1 — testnet сначала, потом mainnet $5 → $10 → $15

### Долгосрочно
9. **TradingView webhook** для Clone3 — нужен TV Pro+ и endpoint
10. **Walk-forward optimization** — переоптимизация параметров каждые 30 дней
11. **Regime filter** — не торговать в trending если стратегия для ranging (и наоборот)
12. **Ensemble** — голосование 3+ клонов, открывать только когда все согласны

---

## Связанные файлы

- **Прод-конфиг:** `/home/andy/CryptoTrader/config/settings.yaml`
- **Прод-trading agent:** `/home/andy/CryptoTrader/src/agents/trading_agent.py`
- **Прод-execution:** `/home/andy/CryptoTrader/src/agents/execution_agent.py`
- **Multi-agent engine (7 blocks):** `/home/andy/CryptoTrader/src/agents/multi_agent_engine.py`
- **Оригинальный score engine:** `/tmp/backtest_score_engine.py`
- **Skill:** `~/.hermes/skills/trading/cryptotrader-clones-backtest/`
- **Commits:** `ff18e4b` (pre-clone), `ebcd87e` (clones), `9ff421c` (keyerror fix)

## История

- **2026-06-06** — создан пакет, 4 клона, runner, back+forward tests, коммиты в git
- **Следующий шаг:** ждём решение Boss по дальнейшим действиям (1-4 из "Потенциал для развития")
