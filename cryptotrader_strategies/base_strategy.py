"""
BaseStrategy — общий интерфейс и утилиты для всех клонов стратегий CryptoTrader.

Клоны:
  - Clone0: текущая прод-стратегия (5m, 3 пары)
  - Clone1: low-risk (15m, 8 пар, SL=1%, TP=1%, trailing 0.1%/5min)
  - Clone2: contrarian (3b: инверсия при conf<0.5; 3c: по сентименту)
  - Clone3: stub (TradingView webhook — пока заглушка)

Каждый клон наследует BaseStrategy и реализует:
  - decide(df, symbol) -> dict
  - параметры (TF, пары, SL, TP, trailing, min_conf)
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# === Индикаторы (правильные реализации, без багов из trading_agent) ===

def compute_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI. Возвращает массив той же длины (первые period = NaN)."""
    c = np.asarray(closes, dtype=float)
    d = np.diff(c, prepend=c[0])
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    out = np.full(len(c), np.nan)
    if len(c) < period + 1:
        return out
    # Wilder smoothing (alpha=1/period)
    avg_g = np.zeros(len(c))
    avg_l = np.zeros(len(c))
    avg_g[period] = np.mean(gain[1:period + 1])
    avg_l[period] = np.mean(loss[1:period + 1])
    for i in range(period + 1, len(c)):
        avg_g[i] = (avg_g[i - 1] * (period - 1) + gain[i]) / period
        avg_l[i] = (avg_l[i - 1] * (period - 1) + loss[i]) / period
    rs = np.divide(avg_g, np.maximum(avg_l, 1e-10), out=np.zeros_like(avg_g), where=avg_l > 0)
    out = 100 - 100 / (1 + rs)
    return out


def compute_ema(series: np.ndarray, span: int) -> np.ndarray:
    return pd.Series(series).ewm(span=span, adjust=False).mean().values


def compute_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    tr = np.zeros(len(close))
    for i in range(1, len(close)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    return pd.Series(tr).ewm(span=period, adjust=False).mean().values


def compute_bollinger(close: np.ndarray, period: int = 20, std: float = 2.0):
    s = pd.Series(close)
    sma = s.rolling(period).mean().values
    sd = s.rolling(period).std().values
    return sma + std * sd, sma, sma - std * sd


def compute_css(close: np.ndarray, fast: int = 12, slow: int = 26) -> np.ndarray:
    """CSS-lite: разница fast-slow EMA, нормализованная по abs(slow)."""
    ema_f = compute_ema(close, fast)
    ema_s = compute_ema(close, slow)
    return (ema_f - ema_s) / (np.abs(ema_s) + 1e-10) * 100


def compute_macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_f = compute_ema(close, fast)
    ema_s = compute_ema(close, slow)
    line = ema_f - ema_s
    sig = pd.Series(line).ewm(span=signal, adjust=False).mean().values
    return line, sig, line - sig


def compute_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """ADX — нужен для определения режима (trending vs ranging)."""
    h = pd.Series(high)
    l = pd.Series(low)
    c = pd.Series(close)
    plus_dm = h.diff()
    minus_dm = -l.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean().fillna(0).values


# === Параметры стратегий ===

@dataclass
class StrategyParams:
    """Параметры конкретной стратегии."""
    name: str
    timeframe: str = "5m"                          # '1m'/'5m'/'15m'/'1h'/'4h'
    symbols: List[str] = field(default_factory=list) # пары
    min_confidence: float = 0.75                    # порог входа
    sl_pct: float = 1.0                             # SL в %
    tp_pct: float = 1.0                             # TP в %
    trailing_enabled: bool = False
    trailing_step_pct: float = 0.1                 # шаг подтягивания
    trailing_interval_min: int = 5                  # как часто проверяем trailing
    max_hold_minutes: int = 240                    # 4ч макс
    max_open_per_symbol: int = 1
    fee_pct: float = 0.055                           # Bybit linear taker 0.055% (round-trip ×2)
    # Contrarian-специфика:
    contrarian_invert_low_conf: bool = False       # 3b: инвертировать при conf<0.5
    contrarian_invert_threshold: float = 0.5
    contrarian_sentiment_invert: bool = False      # 3c: инвертировать по сентименту
    sentiment_bullish_threshold: float = 0.7       # retail >70% bullish → contrarian SELL
    # Mean Reversion-специфика (clone_4):
    adx_max: float = 25.0                          # торгуем только при ADX<=adx_max
    rsi_oversold: float = 30.0                     # RSI<rsi_oversold → LONG
    rsi_overbought: float = 70.0                   # RSI>rsi_overbought → SHORT
    bb_extreme_pct: float = 0.05                   # цена в нижних bb_extreme_pct BB → extreme


# === Базовый класс ===

class BaseStrategy(ABC):
    """Базовый класс для всех клонов стратегий."""

    def __init__(self, params: StrategyParams, logger: Optional[logging.Logger] = None):
        self.params = params
        self.logger = logger or logging.getLogger(f"strategy.{params.name}")

    @abstractmethod
    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Возвращает решение по последнему бару.

        Returns:
            {
                'signal': 'BUY' | 'SELL' | 'HOLD',
                'confidence': 0.0..1.0,
                'reasoning': str,
                'stop_loss_pct': float,
                'take_profit_pct': float,
                'score': float (raw score до инверсии, для отладки),
                'side': 'LONG' | 'SHORT' | None,
                'details': dict,
            }
        """
        raise NotImplementedError

    # === Утилиты: trailing stop в backtest ===

    def apply_trailing(self, side: str, entry: float, high: float, low: float,
                       current_sl: float, current_tp: float, minutes_held: int) -> Tuple[float, float, bool]:
        """Применить trailing stop. Возвращает (new_sl, tp, trailing_activated).

        Логика:
          - Подтягиваем SL только после `trailing_interval_min` (как в требованиях)
          - Шаг: trailing_step_pct (0.1%)
          - Подтягиваем только если цена прошла trailing_step_pct в прибыль
        """
        if not self.params.trailing_enabled:
            return current_sl, current_tp, False

        # Check interval: trailing срабатывает только если прошло >= interval
        if minutes_held < self.params.trailing_interval_min:
            return current_sl, current_tp, False

        if side == 'LONG':
            profit_pct = (high - entry) / entry * 100
            if profit_pct >= self.params.trailing_step_pct:
                # Подтягиваем SL на (profit - step)%
                new_sl = entry * (1 + (profit_pct - self.params.trailing_step_pct) / 100)
                new_sl = max(new_sl, current_sl)  # не опускаем SL
                if new_sl > current_sl:
                    return new_sl, current_tp, True
        elif side == 'SHORT':
            profit_pct = (entry - low) / entry * 100
            if profit_pct >= self.params.trailing_step_pct:
                new_sl = entry * (1 - (profit_pct - self.params.trailing_step_pct) / 100)
                new_sl = min(new_sl, current_sl)
                if new_sl < current_sl:
                    return new_sl, current_tp, True
        return current_sl, current_tp, False

    # === Защита от overfit: sanity check решения ===

    def is_valid_decision(self, decision: Dict[str, Any]) -> bool:
        """Минимальная валидация перед возвратом."""
        sig = decision.get('signal')
        if sig not in ('BUY', 'SELL', 'HOLD'):
            return False
        conf = decision.get('confidence', 0)
        if sig in ('BUY', 'SELL') and conf < self.params.min_confidence:
            return False
        return True

    def contrarian_invert(self, decision: Dict[str, Any],
                          sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Применить contrarian-логику (3b и/или 3c) если включено в params."""
        sig = decision.get('signal', 'HOLD')
        conf = decision.get('confidence', 0.0)

        # 3b: инвертировать при НИЗКОЙ уверенности (сомневается рынок → идём против)
        if self.params.contrarian_invert_low_conf and conf < self.params.contrarian_invert_threshold:
            if sig in ('BUY', 'SELL'):
                new_sig = 'SELL' if sig == 'BUY' else 'BUY'
                self.logger.debug(f"3b: invert {sig}(conf={conf:.2f})→{new_sig}")
                decision = dict(decision)
                decision['signal'] = new_sig
                decision['original_signal'] = sig
                decision['contrarian_reason'] = '3b:low_conf_invert'
                sig = new_sig

        # 3c: инвертировать когда retail sentiment в эйфории/панике
        if self.params.contrarian_sentiment_invert and sentiment:
            sent = sentiment.get('avg_sentiment', 0.0)
            bull_ratio = sentiment.get('bullish_ratio', 0.5)
            if bull_ratio >= self.params.sentiment_bullish_threshold and sig == 'BUY':
                decision = dict(decision)
                decision['signal'] = 'SELL'
                decision['original_signal'] = sig
                decision['contrarian_reason'] = '3c:euphoria_invert'
                self.logger.debug(f"3c: invert BUY→SELL (bull_ratio={bull_ratio:.0%})")
            elif bull_ratio <= (1 - self.params.sentiment_bullish_threshold) and sig == 'SELL':
                decision = dict(decision)
                decision['signal'] = 'BUY'
                decision['original_signal'] = sig
                decision['contrarian_reason'] = '3c:panic_invert'

        return decision
