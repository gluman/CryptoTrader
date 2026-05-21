"""
Position Strategy — 4h/1d/1w timeframe
Long-term swing trades, holds for days/weeks
"""

from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


class PositionStrategy(BaseStrategy):
    """Position/swing trading strategy for 4h/1d/1w trades"""

    def __init__(self):
        super().__init__('Position', self.POSITION)
        self.params = {
            # Risk management
            'max_loss_per_trade': 0.05,           # 5% max loss
            'target_profit': 0.12,                # 12% target
            'max_daily_loss': 0.08,               # 8% max daily loss
            'max_position_size': 0.3,              # 30% of capital per trade

            # RSI
            'rsi_period': 14,
            'rsi_oversold': 35,
            'rsi_overbought': 65,
            'rsi_strong_oversold': 30,
            'rsi_strong_overbought': 70,

            # SMA for trend
            'sma_20': 20,
            'sma_50': 50,
            'sma_200': 200,

            # MACD
            'macd_fast': 12,
            'macd_slow': 26,
            'macd_signal': 9,

            # Volume
            'volume_ma_period': 20,
            'volume_spike': 1.2,

            # Min confidence
            'min_confidence': 0.55,
        }

    def get_timeframes(self) -> List[str]:
        return ['4h', '1d', '1w']

    def get_default_symbols(self) -> List[str]:
        return ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']

    def get_parameters(self) -> Dict[str, Any]:
        return self.params

    def analyze(self,
               df_1m: Optional[pd.DataFrame],
               df_5m: Optional[pd.DataFrame],
               df_15m: Optional[pd.DataFrame],
               df_1h: Optional[pd.DataFrame],
               df_4h: Optional[pd.DataFrame],
               df_1d: Optional[pd.DataFrame],
               indicators: Dict[str, Any]) -> Dict[str, Any]:

        # Primary 1d, confirm with 4h
        if df_1d is None or len(df_1d) < 50:
            return self._hold('Insufficient 1d data')

        df = df_1d.copy()
        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']

        price = close.iloc[-1]

        # RSI
        rsi = self._rsi(close, self.params['rsi_period'])
        rsi_val = rsi.iloc[-1]
        rsi_prior = rsi.iloc[-3] if len(rsi) >= 3 else rsi_val  # 3 days ago

        # SMA trend
        sma_20 = self._sma(close, self.params['sma_20'])
        sma_50 = self._sma(close, self.params['sma_50'])
        sma_200 = self._sma(close, self.params['sma_200']) if len(close) >= 200 else None

        # Trend direction
        price_vs_sma20 = price > sma_20.iloc[-1]
        price_vs_sma50 = price > sma_50.iloc[-1]
        sma20_vs_sma50 = sma_20.iloc[-1] > sma_50.iloc[-1]

        # Strong uptrend: price > SMA20 > SMA50
        strong_uptrend = price_vs_sma20 and sma20_vs_sma50
        strong_downtrend = not price_vs_sma20 and not sma20_vs_sma50

        # MACD on daily
        macd_line, macd_signal_line, macd_hist = self._macd(
            close,
            self.params['macd_fast'],
            self.params['macd_slow'],
            self.params['macd_signal']
        )
        macd_hist_val = macd_hist.iloc[-1]
        macd_hist_prior = macd_hist.iloc[-5] if len(macd_hist) >= 5 else macd_hist_val  # 5 days ago

        # Volume
        vol_ma = volume.rolling(self.params['volume_ma_period']).mean().iloc[-1]
        vol_ratio = volume.iloc[-1] / vol_ma if vol_ma > 0 else 1.0

        # 4h confirmation
        df_4h_confirm = df_4h.copy() if df_4h is not None and len(df_4h) > 0 else None
        confirm_bullish = False
        confirm_bearish = False
        if df_4h_confirm is not None:
            close_4h = df_4h_confirm['close']
            sma_20_4h = self._sma(close_4h, 20)
            sma_50_4h = self._sma(close_4h, 50)
            confirm_bullish = close_4h.iloc[-1] > sma_20_4h.iloc[-1] > sma_50_4h.iloc[-1]
            confirm_bearish = close_4h.iloc[-1] < sma_20_4h.iloc[-1] < sma_50_4h.iloc[-1]

        # Entry signals
        buy_signal = False
        sell_signal = False

        # BUY conditions - stronger thresholds for position trades
        if (
            (rsi_val < self.params['rsi_strong_oversold'] or
             (rsi_val < self.params['rsi_oversold'] and strong_uptrend)) or
            (macd_hist_val > 0 and macd_hist_prior <= 0 and rsi_val < 50) or
            (rsi_val < 40 and price_vs_sma20 and sma20_vs_sma50)
        ):
            if df_4h_confirm is None or confirm_bullish or not confirm_bearish:
                buy_signal = True

        # SELL conditions
        if (
            (rsi_val > self.params['rsi_strong_overbought'] or
             (rsi_val > self.params['rsi_overbought'] and strong_downtrend)) or
            (macd_hist_val < 0 and macd_hist_prior >= 0 and rsi_val > 50) or
            (rsi_val > 60 and not price_vs_sma20 and not sma20_vs_sma50)
        ):
            if df_4h_confirm is None or confirm_bearish or not confirm_bullish:
                sell_signal = True

        # Calculate confidence
        confidence = 0.5
        if buy_signal:
            signals_count = sum([
                rsi_val < self.params['rsi_oversold'],
                strong_uptrend,
                macd_hist_val > 0,
                vol_ratio >= self.params['volume_spike'],
                price_vs_sma50
            ])
            confidence = 0.50 + (signals_count * 0.10)
        elif sell_signal:
            signals_count = sum([
                rsi_val > self.params['rsi_overbought'],
                strong_downtrend,
                macd_hist_val < 0,
                vol_ratio >= self.params['volume_spike'],
                not price_vs_sma50
            ])
            confidence = 0.50 + (signals_count * 0.10)

        confidence = min(confidence, 0.85)

        if confidence < self.params['min_confidence']:
            return self._hold(f'Low confidence: {confidence:.2f}')

        if buy_signal:
            return {
                'action': 'BUY',
                'confidence': round(confidence, 3),
                'entry_price': float(price),
                'stop_loss': round(price * (1 - self.params['max_loss_per_trade']), 8),
                'take_profit': round(price * (1 + self.params['target_profit']), 8),
                'strategy': self.name,
                'timeframes': ['1d', '4h'],
                'reasoning': self._build_reasoning('BUY', rsi_val, macd_hist_val,
                                                    strong_uptrend, vol_ratio, confirm_bullish)
            }
        elif sell_signal:
            return {
                'action': 'SELL',
                'confidence': round(confidence, 3),
                'entry_price': float(price),
                'stop_loss': round(price * (1 + self.params['max_loss_per_trade']), 8),
                'take_profit': round(price * (1 - self.params['target_profit']), 8),
                'strategy': self.name,
                'timeframes': ['1d', '4h'],
                'reasoning': self._build_reasoning('SELL', rsi_val, macd_hist_val,
                                                    strong_downtrend, vol_ratio, confirm_bearish)
            }

        return self._hold('No signal')

    def _rsi(self, close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def _macd(self, close: pd.Series, fast: int, slow: int, signal: int):
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - signal_line
        return macd_line, signal_line, hist

    def _sma(self, series: pd.Series, period: int) -> pd.Series:
        return series.rolling(period).mean()

    def _hold(self, reason: str) -> Dict[str, Any]:
        return {
            'action': 'HOLD',
            'confidence': 0.0,
            'strategy': self.name,
            'reasoning': reason
        }

    def _build_reasoning(self, action: str, rsi: float, macd_hist: float,
                         strong_trend: bool, vol_ratio: float, confirm_4h: bool) -> str:
        return (
            f"Position {action}: RSI={rsi:.1f}, MACD_hist={macd_hist:.4f}, "
            f"Trend={'STRONG_UP' if strong_trend else 'STRONG_DOWN' if strong_trend is False else 'WEAK'}, "
            f"Vol={vol_ratio:.1f}x, 4h_confirm={'YES' if confirm_4h else 'NO'}"
        )
