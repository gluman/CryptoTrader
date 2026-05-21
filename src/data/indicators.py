import pandas as pd
import numpy as np
from typing import Dict, List, Optional

def calculate_indicators(df: pd.DataFrame, indicators: Optional[List[str]] = None) -> Dict[str, pd.Series]:
    """
    Calculate technical indicators using pandas operations.
    Returns dict of indicator name -> Series.
    """
    if df.empty:
        return {}
    
    result = {}
    close = df['close']
    high = df['high']
    low = df['low']
    
    # SMA
    if not indicators or 'SMA' in indicators:
        for period in [20, 50, 200]:
            result[f'SMA_{period}'] = close.rolling(period).mean()
    
    # EMA
    if not indicators or 'EMA' in indicators:
        for period in [12, 26]:
            result[f'EMA_{period}'] = close.ewm(span=period, adjust=False).mean()
    
    # RSI
    if not indicators or 'RSI' in indicators:
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss
        result['RSI_14'] = 100 - (100 / (1 + rs))
    
    # MACD
    if not indicators or 'MACD' in indicators:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        macd_signal = macd.ewm(span=9, adjust=False).mean()
        macd_hist = macd - macd_signal
        result['MACD'] = macd
        result['MACDs_12_26_9'] = macd_signal
        result['MACDh_12_26_9'] = macd_hist
    
    # ATR
    if not indicators or 'ATR' in indicators:
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        result['ATR_14'] = tr.rolling(14).mean()
    
    # Bollinger Bands
    if not indicators or 'BBANDS' in indicators:
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        result['BBU_20_2.0'] = sma20 + (2 * std20)
        result['BBM_20_2.0'] = sma20
        result['BBL_20_2.0'] = sma20 - (2 * std20)
    
    return result
