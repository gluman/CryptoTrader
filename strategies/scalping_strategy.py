"""
Scalping Strategy — 1m/5m timeframe
Fast trades, small targets, tight stops
"""

from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


class ScalpingStrategy(BaseStrategy):
    """Scalping strategy for fast 1m/5m trades"""

    def __init__(self):
        super().__init__('Scalping', self.SCALPING)
        self.params = {
            # Risk management
            'max_loss_per_trade': 0.002,      # 0.2% max loss
            'target_profit': 0.003,            # 0.3% target
            'max_daily_loss': 0.015,           # 1.5% max daily loss
            'max_position_size': 0.1,          # 10% of capital per trade

            # Indicators thresholds
            'rsi_oversold': 35,
            'rsi_overbought': 65,
            'rsi_fast': 5,                     # RSI period for scalping
            'volume_spike': 1.5,               # Volume must be 1.5x average

            # Bollinger
            'bb_period': 10,
            'bb_std': 2,

            # EMA cross
            'ema_fast': 5,
            'ema_slow': 15,

            # Min confidence
            'min_confidence': 0.35,
        }

    def get_timeframes(self) -> List[str]:
        return ['1m', '5m']

    def get_default_symbols(self) -> List[str]:
        return ['BTCUSDT', 'ETHUSDT']

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

        # Use 5m as primary, 1m for entry timing
        if df_5m is None or len(df_5m) < 20:
            return self._hold('Insufficient data')

        df = df_5m.copy()
        if len(df) < self.params['bb_period']:
            return self._hold('Not enough bars')

        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']

        # Fast RSI
        rsi = self._fast_rsi(close, self.params['rsi_fast'])
        rsi_val = rsi.iloc[-1]
        rsi_prior = rsi.iloc[-2] if len(rsi) > 1 else rsi_val

        # Bollinger Bands
        bb = self._bollinger(close, self.params['bb_period'], self.params['bb_std'])
        bb_upper = bb['upper'].iloc[-1]
        bb_lower = bb['lower'].iloc[-1]
        bb_middle = bb['middle'].iloc[-1]

        # EMA Cross
        ema_fast = self._ema(close, self.params['ema_fast'])
        ema_slow = self._ema(close, self.params['ema_slow'])
        ema_cross_up = ema_fast.iloc[-1] > ema_slow.iloc[-1] and ema_fast.iloc[-2] <= ema_slow.iloc[-2]
        ema_cross_down = ema_fast.iloc[-1] < ema_slow.iloc[-1] and ema_fast.iloc[-2] >= ema_slow.iloc[-2]

        # Volume check
        vol_sma = volume.rolling(20).mean().iloc[-1]
        vol_ratio = volume.iloc[-1] / vol_sma if vol_sma > 0 else 1.0
        volume_spike = vol_ratio >= self.params['volume_spike']

        # Price near BB edges
        price = close.iloc[-1]
        bb_position = (price - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5

        # Entry signals
        buy_signal = (
            (rsi_val < self.params['rsi_oversold'] and rsi_prior >= self.params['rsi_oversold']) or  # RSI bounce
            (bb_position < 0.15 and volume_spike) or  # BB lower touch + volume
            ema_cross_up  # EMA bullish cross
        )

        sell_signal = (
            (rsi_val > self.params['rsi_overbought'] and rsi_prior <= self.params['rsi_overbought']) or  # RSI reversal
            (bb_position > 0.85 and volume_spike) or   # BB upper touch + volume
            ema_cross_down  # EMA bearish cross
        )

        # Calculate confidence
        confidence = 0.5
        if buy_signal:
            signals_count = sum([
                rsi_val < self.params['rsi_oversold'],
                bb_position < 0.15,
                volume_spike,
                ema_cross_up
            ])
            confidence = 0.55 + (signals_count * 0.1)
        elif sell_signal:
            signals_count = sum([
                rsi_val > self.params['rsi_overbought'],
                bb_position > 0.85,
                volume_spike,
                ema_cross_down
            ])
            confidence = 0.55 + (signals_count * 0.1)

        confidence = min(confidence, 0.90)

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
                'timeframes': ['1m', '5m'],
                'reasoning': self._build_reasoning('BUY', rsi_val, bb_position, vol_ratio, ema_cross_up)
            }
        elif sell_signal:
            return {
                'action': 'SELL',
                'confidence': round(confidence, 3),
                'entry_price': float(price),
                'stop_loss': round(price * (1 + self.params['max_loss_per_trade']), 8),
                'take_profit': round(price * (1 - self.params['target_profit']), 8),
                'strategy': self.name,
                'timeframes': ['1m', '5m'],
                'reasoning': self._build_reasoning('SELL', rsi_val, bb_position, vol_ratio, ema_cross_down)
            }

        return self._hold('No signal')

    def _fast_rsi(self, close: pd.Series, period: int = 5) -> pd.Series:
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def _bollinger(self, close: pd.Series, period: int, std: float) -> Dict[str, pd.Series]:
        middle = close.rolling(period).mean()
        std_dev = close.rolling(period).std()
        return {
            'upper': middle + (std * std_dev),
            'middle': middle,
            'lower': middle - (std * std_dev)
        }

    def _ema(self, series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    def _hold(self, reason: str) -> Dict[str, Any]:
        return {
            'action': 'HOLD',
            'confidence': 0.0,
            'strategy': self.name,
            'reasoning': reason
        }

    def _build_reasoning(self, action: str, rsi: float, bb_pos: float,
                         vol_ratio: float, ema_cross: bool) -> str:
        return (
            f"Scalping {action}: RSI={rsi:.1f}, BB_pos={bb_pos:.2f}, "
            f"Vol_ratio={vol_ratio:.1f}x, EMA_cross={ema_cross}"
        )
