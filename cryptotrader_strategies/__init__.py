"""
Пакет cryptotrader_strategies — 4 клона стратегий для параллельного тестирования.

Clone 0 (clone_0_current):    Текущая прод-стратегия (5m, 3 пары, conf=0.75)
Clone 1 (clone_1_low_risk):   Low-risk 15m, 8 пар, conf=0.5, SL=TP=1%, trailing 0.1%/5min
Clone 2 (clone_2_contrarian): 3b (invert at conf<0.5) + 3c (sentiment invert)
Clone 3 (clone_3_stub):       TradingView webhook заглушка
"""
from .clone_0_current import Clone0CurrentStrategy
from .clone_1_low_risk import Clone1LowRiskStrategy
from .clone_2_contrarian import Clone2ContrarianStrategy
from .clone_3_stub import Clone3StubStrategy


def get_all_strategies(logger=None):
    """Возвращает dict всех клонов (ключ — name)."""
    return {
        Clone0CurrentStrategy.PARAMS.name: Clone0CurrentStrategy(logger=logger),
        Clone1LowRiskStrategy.PARAMS.name: Clone1LowRiskStrategy(logger=logger),
        "clone2_contrarian_3b": Clone2ContrarianStrategy(mode="3b", logger=logger),
        "clone2_contrarian_3c": Clone2ContrarianStrategy(mode="3c", logger=logger),
        Clone3StubStrategy.PARAMS.name: Clone3StubStrategy(logger=logger),
    }


__all__ = [
    "Clone0CurrentStrategy",
    "Clone1LowRiskStrategy",
    "Clone2ContrarianStrategy",
    "Clone3StubStrategy",
    "get_all_strategies",
]
