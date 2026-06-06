"""
Multi-TF Confluence helper — подготовка данных вне decide().

Использование в backtest:
  cache = MultiTFCache(['5m', '15m'])
  for each bar:
      sig_15m = cache.get_15m_signal_at(symbol, current_5m_idx)

Идея: предзагрузить все данные один раз, потом O(1) lookup по индексу.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .base_strategy import (
    compute_adx,
    compute_ema,
)
from .run_backtest import load_ohlcv


class MultiTFCache:
    """Предзагружает 15m данные и кэширует 15m direction на каждый 5m бар."""

    def __init__(self, primary_tf: str = "5m", confirm_tf: str = "15m"):
        self.primary_tf = primary_tf
        self.confirm_tf = confirm_tf
        # {symbol: {'5m': df, '15m': df_with_direction_col}}
        self._cache: Dict[str, Dict[str, pd.DataFrame]] = {}

    def preload(self, symbol: str, start, end) -> None:
        """Загрузить оба TF и посчитать 15m direction per bar."""
        df_5m = load_ohlcv(symbol, self.primary_tf, start, end)
        df_15m = load_ohlcv(symbol, self.confirm_tf, start, end)
        if df_5m.empty or df_15m.empty:
            return

        # 15m direction: per-bar (последний 15m бар, доступный на этот момент)
        closes_15 = df_15m["close"].values
        highs_15 = df_15m["high"].values
        lows_15 = df_15m["low"].values
        ema9_15 = compute_ema(closes_15, 9)
        ema21_15 = compute_ema(closes_15, 21)
        ema50_15 = compute_ema(closes_15, 50)
        adx_15 = compute_adx(highs_15, lows_15, closes_15, 14)

        # Для каждого 5m бара: какой 15m бар последний? (5m idx // 3 = 15m idx)
        # df_5m имеет datetime index. df_15m имеет datetime index. Resample 15m к 5m.
        df_15m["direction"] = 0
        df_15m["adx_15"] = 0.0
        for i in range(len(df_15m)):
            ema_bull = ema9_15[i] > ema21_15[i] and closes_15[i] > ema50_15[i]
            ema_bear = ema9_15[i] < ema21_15[i] and closes_15[i] < ema50_15[i]
            if ema_bull:
                df_15m.iloc[i, df_15m.columns.get_loc("direction")] = 1
            elif ema_bear:
                df_15m.iloc[i, df_15m.columns.get_loc("direction")] = -1
            df_15m.iloc[i, df_15m.columns.get_loc("adx_15")] = (
                float(adx_15[i]) if not np.isnan(adx_15[i]) else 0.0
            )

        # Forward-fill 15m direction на 5m индекс
        df_5m = df_5m.copy()
        # 15m ресэмпл к 5m (ffill)
        df_15m_5m = df_15m[["direction", "adx_15"]].reindex(df_5m.index, method="ffill")
        df_5m["mtf_15m_direction"] = df_15m_5m["direction"].fillna(0).astype(int)
        df_5m["mtf_15m_adx"] = df_15m_5m["adx_15"].fillna(0.0)

        self._cache[symbol] = {"5m": df_5m, "15m": df_15m}

    def get_5m_with_mtf(self, symbol: str) -> pd.DataFrame:
        """Получить 5m DataFrame с колонками mtf_15m_direction, mtf_15m_adx."""
        if symbol not in self._cache:
            return pd.DataFrame()
        return self._cache[symbol]["5m"]

    def get_15m_signal(self, symbol: str) -> Dict[str, Any]:
        """Свежий 15m signal (для отладки)."""
        if symbol not in self._cache:
            return {"direction": 0, "adx": 0, "reason": "no_data"}
        df = self._cache[symbol]["15m"]
        if df.empty:
            return {"direction": 0, "adx": 0, "reason": "empty"}
        last_dir = int(df["direction"].iloc[-1]) if "direction" in df.columns else 0
        last_adx = float(df["adx_15"].iloc[-1]) if "adx_15" in df.columns else 0.0
        return {"direction": last_dir, "adx": last_adx}


def make_mtf_decorator(strategy_class, mtf_cache: MultiTFCache, min_15m_adx: float = 0.0):
    """
    Возвращает обёртку вокруг decide() которая фильтрует сигналы
    по 15m direction.
    """
    original_decide = strategy_class.decide

    def wrapper(self, df, symbol, sentiment=None):
        dec = original_decide(self, df, symbol, sentiment)
        if dec["signal"] == "HOLD":
            return dec

        # Берём 15m direction для этого символа/бара
        if symbol not in mtf_cache._cache:
            return dec  # нет данных — пропускаем фильтр

        # Найти текущий 5m бар в кэше по индексу
        cache_5m = mtf_cache.get_5m_with_mtf(symbol)
        if cache_5m.empty or df.empty:
            return dec

        # Последний бар в df
        last_ts = df.index[-1]
        if last_ts in cache_5m.index:
            row = cache_5m.loc[last_ts]
            mtf_dir = int(row["mtf_15m_direction"]) if "mtf_15m_direction" in row else 0
            mtf_adx = float(row["mtf_15m_adx"]) if "mtf_15m_adx" in row else 0.0
        else:
            # ffill
            prior = cache_5m[cache_5m.index <= last_ts]
            if prior.empty:
                return dec
            row = prior.iloc[-1]
            mtf_dir = int(row["mtf_15m_direction"]) if "mtf_15m_direction" in prior.columns else 0
            mtf_adx = float(row["mtf_15m_adx"]) if "mtf_15m_adx" in prior.columns else 0.0

        if mtf_adx < min_15m_adx:
            return dec  # 15m flat — пропускаем

        if dec["side"] == "LONG" and mtf_dir != 1:
            dec["signal"] = "HOLD"
            dec["side"] = None
            dec["reasoning"] += f" [MTF:15m_dir={mtf_dir},no_long]"
            dec["confidence"] = 0.0
        elif dec["side"] == "SHORT" and mtf_dir != -1:
            dec["signal"] = "HOLD"
            dec["side"] = None
            dec["reasoning"] += f" [MTF:15m_dir={mtf_dir},no_short]"
            dec["confidence"] = 0.0
        else:
            dec["reasoning"] += f" [MTF:15m_dir={mtf_dir}✓,adx={mtf_adx:.0f}]"

        return dec

    strategy_class.decide = wrapper
    return strategy_class

