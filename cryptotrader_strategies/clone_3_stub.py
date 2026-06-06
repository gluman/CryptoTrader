"""
Clone #3 — Stub / заглушка для TradingView webhook (4d).

Что нужно для активации (отложено до решения по источнику):
  1. TradingView Pro+ с настроенными alert-webhook'ами
  2. Endpoint POST /api/v1/tv_signal в нашем API (src/api/server.py)
  3. Список "проверенных" TV-юзеров и маппинг их TV-strategy → symbol
  4. Хранилище сигналов (таблица tv_signals в PostgreSQL)

Сейчас: возвращает HOLD всегда, в details помечает "stub: no TV webhook yet".
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from .base_strategy import BaseStrategy, StrategyParams


class Clone3StubStrategy(BaseStrategy):
    """Клон #3: заглушка до появления TV webhook."""

    PARAMS = StrategyParams(
        name="clone3_tv_stub",
        timeframe="15m",
        symbols=[
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
            "DOGEUSDT", "TONUSDT", "AVAXUSDT", "ADAUSDT",
        ],
        min_confidence=1.0,          # чтобы ничего не открылось
        sl_pct=1.0, tp_pct=2.0,
        fee_pct=0.1,
    )

    def __init__(self, logger=None):
        super().__init__(self.PARAMS, logger)

    def decide(self, df: pd.DataFrame, symbol: str,
               sentiment: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Всегда HOLD. Реальные сигналы придут через webhook → TVSignal row в БД → ExecutionAgent."""
        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "side": None,
            "reasoning": "stub:4d_tradingview_webhook_not_implemented",
            "stop_loss_pct": 0.0,
            "take_profit_pct": 0.0,
            "score": 0.0,
            "regime": "stub",
            "details": {
                "status": "awaiting_tv_webhook",
                "needed": [
                    "TradingView Pro+ account",
                    "Webhook endpoint POST /api/v1/tv_signal",
                    "List of verified TV traders and their strategy → symbol map",
                    "DB table tv_signals (id, symbol, signal, confidence, source, ts)",
                ],
            },
        }
