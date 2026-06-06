"""
Clone #2 — Contrarian. Два режима (3b и 3c) переключаются через params.

3b: инвертировать сигнал при НИЗКОЙ уверенности LLM (conf<0.5).
    Логика: "рынок сомневается — идём против, толпа переоценивает направление"

3c: инвертировать когда retail sentiment в эйфории (bull_ratio>0.7) и
    LLM говорит BUY, либо в панике (bull_ratio<0.3) и LLM говорит SELL.
    Логика: "толпа в эйфории/панике → разворот"

Используется тот же score engine что Clone0, но contrarian-логика применяется
НАД решением (post-processing).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from .base_strategy import (
    BaseStrategy,
    StrategyParams,
    compute_adx,
    compute_atr,
    compute_bollinger,
    compute_css,
    compute_ema,
    compute_macd,
    compute_rsi,
)
from .clone_0_current import Clone0CurrentStrategy


class Clone2ContrarianStrategy(BaseStrategy):
    """Клон #2: тот же score engine что Clone0, но contrarian-фильтр сверху.

    3b: invert if conf<0.5
    3c: invert if retail sentiment extreme
    """

    # По умолчанию — 3b (low-conf invert). Можно переключить на 3c через set_mode().
    PARAMS_3B = StrategyParams(
        name="clone2_contrarian_3b",
        timeframe="5m",
        symbols=["XRPUSDT", "DOGEUSDT", "TONUSDT"],
        min_confidence=0.50,         # пониже, чтобы больше инверсий
        sl_pct=0.30, tp_pct=0.50,
        trailing_enabled=True,
        trailing_step_pct=0.10, trailing_interval_min=5,
        max_hold_minutes=240,
        fee_pct=0.1,
        contrarian_invert_low_conf=True,
        contrarian_invert_threshold=0.5,  # invert при conf<0.5
    )

    PARAMS_3C = StrategyParams(
        name="clone2_contrarian_3c",
        timeframe="5m",
        symbols=["XRPUSDT", "DOGEUSDT", "TONUSDT"],
        min_confidence=0.60,
        sl_pct=0.30, tp_pct=0.50,
        trailing_enabled=True,
        trailing_step_pct=0.10, trailing_interval_min=5,
        max_hold_minutes=240,
        fee_pct=0.1,
        contrarian_invert_low_conf=False,
        contrarian_sentiment_invert=True,
        sentiment_bullish_threshold=0.7,
    )

    def __init__(self, mode: str = "3b", logger=None):
        params = self.PARAMS_3B if mode == "3b" else self.PARAMS_3C
        super().__init__(params, logger)
        self.mode = mode
        # Используем Clone0 как источник сырого решения
        self._base = Clone0CurrentStrategy(logger=logger)

    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        # 1. Сырое решение от Clone0
        raw = self._base.decide(df, symbol, sentiment)

        # 2. Применяем contrarian-фильтр
        decision = self.contrarian_invert(raw, sentiment)

        # 3. Помечаем режим
        decision["clone_mode"] = self.mode
        if "original_signal" in decision and decision.get("signal") != "HOLD":
            decision["reasoning"] = f"[{self.mode} invert] " + decision.get("reasoning", "")

        return decision
