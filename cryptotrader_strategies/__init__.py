"""
Пакет cryptotrader_strategies — 6 клонов стратегий для параллельного тестирования.

Clone 0 (clone_0_current):        Текущая BB Squeeze Breakout (5m, 3 пары, R:R=5) — В ПЛЮСЕ
Clone 1 (clone_1_low_risk):       BB Squeeze Breakout (15m, 6 high-vol пар)
Clone 2 (clone_2_contrarian):     3b (invert at conf<0.5) + 3c (sentiment invert)
Clone 3 (clone_3_stub):           TradingView webhook заглушка
Clone 4 (clone_4_mean_reversion): Mean Reversion (15m, 8 пар, ranging only, BB+RSI, R:R=2)
Clone 5 (clone_5_market_maker):   Market Maker / Liquidity Hunt стратегия (v1)
Clone 5 v2 (clone_5_v2_ict):      Market Maker + ICT 2022 Mentorship rules (FVG, EQH, PO3)
"""
from .clone_0_current import Clone0CurrentStrategy
from .clone_1_low_risk import Clone1LowRiskStrategy
from .clone_2_contrarian import Clone2ContrarianStrategy
from .clone_3_stub import Clone3StubStrategy
from .clone_4_mean_reversion import Clone4MeanReversionStrategy
from .clone_5_market_maker import Clone5MarketMakerStrategy
from .clone_5_v2_ict import Clone5V2Strategy
from .clone_5_v6 import Clone5V6Strategy


def get_all_strategies(logger=None):
    """Возвращает dict всех клонов (ключ — name)."""
    return {
        Clone0CurrentStrategy.PARAMS.name: Clone0CurrentStrategy(logger=logger),
        Clone1LowRiskStrategy.PARAMS.name: Clone1LowRiskStrategy(logger=logger),
        "clone2_contrarian_3b": Clone2ContrarianStrategy(mode="3b", logger=logger),
        "clone2_contrarian_3c": Clone2ContrarianStrategy(mode="3c", logger=logger),
        Clone3StubStrategy.PARAMS.name: Clone3StubStrategy(logger=logger),
        Clone4MeanReversionStrategy.PARAMS.name: Clone4MeanReversionStrategy(logger=logger),
        Clone5MarketMakerStrategy.PARAMS.name: Clone5MarketMakerStrategy(logger=logger),
        Clone5V2Strategy.PARAMS.name: Clone5V2Strategy(logger=logger),
        Clone5V6Strategy.PARAMS.name: Clone5V6Strategy(logger=logger),
    }


__all__ = [
    "Clone0CurrentStrategy",
    "Clone1LowRiskStrategy",
    "Clone2ContrarianStrategy",
    "Clone3StubStrategy",
    "Clone4MeanReversionStrategy",
    "Clone5MarketMakerStrategy",
    "Clone5V2Strategy",
    "Clone5V6Strategy",
    "get_all_strategies",
]
