"""
Intraday Strategy — 15m/1h/4h timeframe
Medium-term trades within single day
"""

from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


class IntradayStrategy(BaseStrategy):
    """Intraday trading strategy for 15m/1h/4h trades"""

    def __init__(self):
        super().__init__('Intraday', self.INTRADAY)
        self.params = {
            # Risk management
            'max_loss_per_trade': 0.015,       # 1.5% max loss
            'target_profit': 0.025,            # 2.5% target
            'max_daily_loss': 0.03,            # 3% max daily loss
            'max_position_size': 0.2,          # 20% of capital per trade

            # RSI
            'rsi_period': 14,
            'rsi_oversold': 40,
            'rsi_overbought': 60,

            # MACD
            'macd_fast': 12,
            'macd_slow': 26,
            'macd_signal': 9,

            # EMA
            'ema_20': 20,
            'ema_50': 50,

            # Volume
            'volume_ma_period': 20,
            'volume_spike': 1.3,

            # Min confidence
            'min_confidence': 0.60,
        }

    def get_timeframes(self) -> List[str]:
        return ['15m', '1h', '4h']

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

        # Primary timeframe 1h, confirm with 4h
        if df_1h is None or len(df_1h) < 50:
            return self._hold('Insufficient 1h data')

        df = df_1h.copy()
        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']

        price = close.iloc[-1]

        # RSI
        rsi = self._rsi(close, self.params['rsi_period'])
        rsi_val = rsi.iloc[-1]
        rsi_prior = rsi.iloc[-2] if len(rsi) > 1 else rsi_val
        rsi_5_ago = rsi.iloc[-5] if len(rsi) >= 5 else rsi_val

        # MACD
        macd_line, macd_signal_line, macd_hist = self._macd(
            close,
            self.params['macd_fast'],
            self.params['macd_slow'],
            self.params['macd_signal']
        )
        macd_val = macd_line.iloc[-1]
        macd_sig = macd_signal_line.iloc[-1]
        macd_hist_val = macd_hist.iloc[-1]
        macd_hist_prior = macd_hist.iloc[-2] if len(macd_hist) > 1 else macd_hist_val

        # MACD crossover
        macd_cross_up = macd_hist_val > 0 and macd_hist_prior <= 0
        macd_cross_down = macd_hist_val < 0 and macd_hist_prior >= 0

        # EMA trend
        ema_20 = self._ema(close, self.params['ema_20'])
        ema_50 = self._ema(close, self.params['ema_50'])
        ema_trend_up = ema_20.iloc[-1] > ema_50.iloc[-1]
        ema_trend_down = ema_20.iloc[-1] < ema_50.iloc[-1]

        # Volume
        vol_ma = volume.rolling(self.params['volume_ma_period']).mean().iloc[-1]
        vol_ratio = volume.iloc[-1] / vol_ma if vol_ma > 0 else 1.0

        # 4h confirmation
        df_4h_confirm = df_4h.copy() if df_4h is not None and len(df_4h) > 0 else None
        confirm_bullish = False
        confirm_bearish = False
        if df_4h_confirm is not None:
            close_4h = df_4h_confirm['close']
            ema_20_4h = self._ema(close_4h, 20)
            ema_50_4h = self._ema(close_4h, 50)
            confirm_bullish = close_4h.iloc[-1] > ema_20_4h.iloc[-1] > ema_50_4h.iloc[-1]
            confirm_bearish = close_4h.iloc[-1] < ema_20_4h.iloc[-1] < ema_50_4h.iloc[-1]

        # Entry signals
        buy_signal = False
        sell_signal = False

        # BUY conditions
        if (
            (rsi_val < self.params['rsi_oversold'] and rsi_prior >= self.params['rsi_oversold']) or  # RSI bounce
            (macd_cross_up and rsi_val < 50) or  # MACD bullish cross with RSI not overbought
            (rsi_val < 45 and ema_trend_up)  # RSI neutral + EMA bullish
        ):
            # Confirm with 4h if available
            if df_4h_confirm is None or confirm_bullish:
                buy_signal = True

        # SELL conditions
        if (
            (rsi_val > self.params['rsi_overbought'] and rsi_prior <= self.params['rsi_overbought']) or  # RSI reversal
            (macd_cross_down and rsi_val > 50) or  # MACD bearish cross with RSI not oversold
            (rsi_val > 55 and ema_trend_down)  # RSI neutral + EMA bearish
        ):
            # Confirm with 4h if available
            if df_4h_confirm is None or confirm_bearish:
                sell_signal = True

        # Calculate confidence
        confidence = 0.5
        if buy_signal:
            signals_count = sum([
                rsi_val < self.params['rsi_oversold'],
                macd_cross_up,
                ema_trend_up,
                vol_ratio >= self.params['volume_spike'],
                confirm_bullish if df_4h_confirm is not None else True
            ])
            confidence = 0.55 + (signals_count * 0.08)
        elif sell_signal:
            signals_count = sum([
                rsi_val > self.params['rsi_overbought'],
                macd_cross_down,
                ema_trend_down,
                vol_ratio >= self.params['volume_spike'],
                confirm_bearish if df_4h_confirm is not None else True
            ])
            confidence = 0.55 + (signals_count * 0.08)

        confidence = min(confidence, 0.88)

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
                'timeframes': ['1h', '4h'],
                'reasoning': self._build_reasoning('BUY', rsi_val, macd_hist_val,
                                                    ema_trend_up, vol_ratio, confirm_bullish)
            }
        elif sell_signal:
            return {
                'action': 'SELL',
                'confidence': round(confidence, 3),
                'entry_price': float(price),
                'stop_loss': round(price * (1 + self.params['max_loss_per_trade']), 8),
                'take_profit': round(price * (1 - self.params['target_profit']), 8),
                'strategy': self.name,
                'timeframes': ['1h', '4h'],
                'reasoning': self._build_reasoning('SELL', rsi_val, macd_hist_val,
                                                    ema_trend_down, vol_ratio, confirm_bearish)
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

    def _ema(self, series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    def _hold(self, reason: str) -> Dict[str, Any]:
        return {
            'action': 'HOLD',
            'confidence': 0.0,
            'strategy': self.name,
            'reasoning': reason
        }

    def _build_reasoning(self, action: str, rsi: float, macd_hist: float,
                         ema_trend: bool, vol_ratio: float, confirm_4h: bool) -> str:
        return (
            f"Intraday {action}: RSI={rsi:.1f}, MACD_hist={macd_hist:.4f}, "
            f"EMA_trend={'UP' if ema_trend else 'DOWN'}, Vol={vol_ratio:.1f}x, "
            f"4h_confirm={'YES' if confirm_4h else 'NO'}"
        )
