import pandas as pd
import numpy as np
from typing import List, Optional

class CurrencySlopeStrength:
    """
    Currency Slope Strength indicator — Python port from MQL.
    Based on GA_CSS_2.mq5 by Deltabron.
    
    Calculates the relative strength of a currency based on the slope 
    of its moving average normalized by ATR.
    """
    
    def __init__(self, lookback: int = 200, ma_period: int = 20):
        self.lookback = lookback
        self.ma_period = ma_period
    
    def calculate(self, df: pd.DataFrame, timeframes: Optional[List[str]] = None) -> pd.Series:
        """
        Calculate CSS value for a symbol.
        
        Args:
            df: DataFrame with OHLCV data (must have 'close', 'high', 'low')
            timeframes: List of timeframes (used for multi-TF analysis, currently single-TF)
        
        Returns:
            Series of CSS values normalized to [-1, 1]
        """
        if df.empty or len(df) < self.ma_period + 1:
            return pd.Series(dtype=float)
        
        close = df['close']
        high = df['high']
        low = df['low']
        
        # Calculate MA
        ma = close.rolling(window=self.ma_period).mean()
        
        # Calculate ATR for normalization
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(window=14).mean()
        
        # Calculate slope (change in MA per bar)
        slope = ma.diff()
        
        # Normalize by ATR to get relative strength
        css = slope / atr.replace(0, np.nan)
        
        # Normalize to [-1, 1] range using rolling min-max
        css_rolling = css.rolling(window=self.lookback)
        css_min = css_rolling.min()
        css_max = css_rolling.max()
        css_range = css_max - css_min
        
        css_normalized = 2 * (css - css_min) / css_range.replace(0, np.nan) - 1
        
        return css_normalized.fillna(0)
    
    def get_signal(self, css: pd.Series, level_cross: float = 0.20) -> pd.Series:
        """
        Generate trading signal based on CSS level crossing.
        
        Args:
            css: Series of CSS values
            level_cross: Level to cross for signal generation
        
        Returns:
            Series of signals: 'BUY', 'SELL', or 'HOLD'
        """
        if len(css) < 2:
            return pd.Series('HOLD', index=css.index)
        
        prev = css.shift(1)
        
        signal = pd.Series('HOLD', index=css.index)
        signal[(prev < level_cross) & (css >= level_cross)] = 'BUY'
        signal[(prev > -level_cross) & (css <= -level_cross)] = 'SELL'
        
        return signal
