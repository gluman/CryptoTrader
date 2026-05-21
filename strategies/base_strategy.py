from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import pandas as pd


class BaseStrategy(ABC):
    """Base class for all trading strategies"""

    SCALPING = 'scalping'
    INTRADAY = 'intraday'
    POSITION = 'position'

    def __init__(self, name: str, strategy_type: str):
        self.name = name
        self.strategy_type = strategy_type

    @abstractmethod
    def analyze(self,
                df_1m: Optional[pd.DataFrame],
                df_5m: Optional[pd.DataFrame],
                df_15m: Optional[pd.DataFrame],
                df_1h: Optional[pd.DataFrame],
                df_4h: Optional[pd.DataFrame],
                df_1d: Optional[pd.DataFrame],
                indicators: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze and return signal"""
        pass

    @abstractmethod
    def get_timeframes(self) -> List[str]:
        """Return required timeframes for this strategy"""
        pass

    @abstractmethod
    def get_default_symbols(self) -> List[str]:
        """Return default symbols for this strategy"""
        pass

    @abstractmethod
    def get_parameters(self) -> Dict[str, Any]:
        """Return strategy parameters (SL, TP, etc)"""
        pass

    def validate_signal(self, signal: Dict[str, Any]) -> bool:
        """Validate signal before execution"""
        if signal.get('action') == 'HOLD':
            return True
        required = ['action', 'confidence', 'symbol']
        return all(k in signal for k in required)
