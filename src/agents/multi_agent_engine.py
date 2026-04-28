"""
Multi-Agent Decision Engine — 6 блоков:
1. TechnicalAnalysis   (вес 0.25) — RSI, MACD, BB, SMA, CSS
2. FundamentalAnalysis (вес 0.15) — долгосрочные факторы
3. PatternRecognition  (вес 0.20) — свечные паттерны
4. NewsAnalysis        (вес 0.15) — новостной фон
5. MarketSentiment     (вес  0.10) — настроения рынка
6. PumpHunter          (вес 0.15) — стратегия Pump Hunter

Каждый блок возвращает: {score: 0.0-1.0, signal: BUY/SELL/HOLD, reasoning: str}
Aggregator: weighted sum → final decision
"""

import pandas as pd
import numpy as np
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from .base import BaseAgent
from ..core.config import Config
from sqlalchemy import text, func


# === Block 1: Technical Analysis ===

class TechnicalAnalysisAgent:
    """Блок 1: Технический анализ — RSI, MACD, Bollinger Bands, CSS"""
    
    def __init__(self, config: Config, logger: logging.Logger):
        self.log = logger
        self.config = config
    
    def analyze(self, df: pd.DataFrame, symbol: str) -> Dict[str, Any]:
        if df.empty or len(df) < 50:
            return {'score': 0.0, 'signal': 'HOLD', 'reasoning': 'Insufficient data', 'block': 'technical'}
        
        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']
        
        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = float(rsi.iloc[-1])
        rsi_prior = float(rsi.iloc[-2]) if len(rsi) > 1 else rsi_val
        
        # MACD
        ema_12 = close.ewm(span=12).mean()
        ema_26 = close.ewm(span=26).mean()
        macd = ema_12 - ema_26
        macd_signal = macd.ewm(span=9).mean()
        macd_hist = macd - macd_signal
        macd_val = float(macd.iloc[-1])
        macd_hist_val = float(macd_hist.iloc[-1])
        macd_hist_prior = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else macd_hist_val
        
        # Bollinger Bands
        sma_20 = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = sma_20 + 2 * bb_std
        bb_lower = sma_20 - 2 * bb_std
        price = float(close.iloc[-1])
        bb_upper_val = float(bb_upper.iloc[-1])
        bb_lower_val = float(bb_lower.iloc[-1])
        
        # SMA trend
        sma_20_val = float(sma_20.iloc[-1])
        sma_50 = close.rolling(50).mean()
        sma_50_val = float(sma_50.iloc[-1]) if len(sma_50) >= 50 else sma_20_val
        
        # Volume
        vol_sma = volume.rolling(20).mean().iloc[-1]
        vol_ratio = float(volume.iloc[-1] / vol_sma) if vol_sma > 0 else 1.0
        
        # CSS
        ma_period = self.config.css_indicator.get('sma_period', 20)
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        ma = close.rolling(ma_period).mean()
        slope = (ma - ma.shift(1)) / atr if atr > 0 else ma - ma.shift(1)
        css = float(slope.iloc[-1])
        css_prior = float(slope.iloc[-2]) if len(slope) > 1 else css
        
        # === SCORING ===
        score = 0.5
        bullish_signals = 0
        bearish_signals = 0
        
        # RSI
        if rsi_val < 30:
            bullish_signals += 2
        elif rsi_val > 70:
            bearish_signals += 2
        elif rsi_val < 45:
            bullish_signals += 1
        elif rsi_val > 55:
            bearish_signals += 1
        
        # MACD histogram direction
        if macd_hist_val > 0 and macd_hist_val > macd_hist_prior:
            bullish_signals += 2
        elif macd_hist_val < 0 and macd_hist_val < macd_hist_prior:
            bearish_signals += 2
        elif macd_hist_val > macd_hist_prior:
            bullish_signals += 1
        else:
            bearish_signals += 1
        
        # Price vs SMA
        if price > sma_20_val:
            bullish_signals += 1
        else:
            bearish_signals += 1
        if price > sma_50_val:
            bullish_signals += 2
        else:
            bearish_signals += 2
        
        # Bollinger position
        bb_range = bb_upper_val - bb_lower_val
        if bb_range > 0:
            bb_pos = (price - bb_lower_val) / bb_range
            if bb_pos < 0.2:
                bullish_signals += 2  # near lower band = oversold
            elif bb_pos > 0.8:
                bearish_signals += 2  # near upper band = overbought
        
        # CSS trend
        level = self.config.css_indicator.get('level_trade', 0.20)
        if css > level:
            bullish_signals += 2
        elif css < -level:
            bearish_signals += 2
        elif css > css_prior:
            bullish_signals += 1
        else:
            bearish_signals += 1
        
        # Volume confirmation
        if vol_ratio > 1.5:
            if bullish_signals > bearish_signals:
                bullish_signals += 1
            else:
                bearish_signals += 1
        
        total = bullish_signals + bearish_signals
        if total > 0:
            score = bullish_signals / total
        
        # Determine signal
        if score >= 0.65:
            signal = 'BUY'
        elif score <= 0.35:
            signal = 'SELL'
        else:
            signal = 'HOLD'
        
        reasoning = (
            f"RSI={rsi_val:.1f}, MACD_hist={macd_hist_val:.6f}, "
            f"price={price:.4f}, CSS={css:.4f}, vol_ratio={vol_ratio:.2f}x"
        )
        
        self.log.debug(f"Technical: score={score:.2f}, signal={signal}, {reasoning}")
        
        return {
            'score': round(score, 4),
            'signal': signal,
            'reasoning': reasoning,
            'block': 'technical',
            'details': {
                'rsi': rsi_val,
                'macd_hist': macd_hist_val,
                'css': css,
                'price': price,
                'sma_20': sma_20_val,
                'sma_50': sma_50_val,
                'bb_upper': bb_upper_val,
                'bb_lower': bb_lower_val,
                'vol_ratio': vol_ratio,
            }
        }


# === Block 2: Fundamental Analysis ===

class FundamentalAnalysisAgent:
    """Блок 2: Фундаментальный анализ — долгосрочные паттерны, объёмы, кросс-корреляции"""
    
    def __init__(self, config: Config, logger: logging.Logger):
        self.log = logger
        self.config = config
    
    def analyze(self, df: pd.DataFrame, symbol: str) -> Dict[str, Any]:
        if df.empty or len(df) < 100:
            return {'score': 0.5, 'signal': 'HOLD', 'reasoning': 'Insufficient data', 'block': 'fundamental'}
        
        close = df['close']
        volume = df['volume']
        
        # Trend strength over longer window
        sma_20 = close.rolling(20).mean()
        sma_50 = close.rolling(50).mean()
        price = float(close.iloc[-1])
        
        # Volatility
        returns = close.pct_change().dropna()
        volatility = float(returns.std() * 100)
        
        # Volume trend
        vol_current = float(volume.iloc[-20:].mean())
        vol_historical = float(volume.iloc[-100:-20].mean()) if len(volume) >= 100 else vol_current
        vol_trend = vol_current / vol_historical if vol_historical > 0 else 1.0
        
        # Trend alignment (price above/below major SMAs)
        bullish = 0
        bearish = 0
        
        if len(sma_20) >= 20:
            if price > sma_20.iloc[-1]:
                bullish += 1
            else:
                bearish += 1
        if len(sma_50) >= 50:
            if price > sma_50.iloc[-1]:
                bullish += 2
            else:
                bearish += 2
        
        # Momentum over 20 periods
        momentum = float((close.iloc[-1] / close.iloc[-20] - 1) * 100) if len(close) >= 20 else 0
        if momentum > 5:
            bullish += 2
        elif momentum < -5:
            bearish += 2
        elif momentum > 0:
            bullish += 1
        else:
            bearish += 1
        
        total = bullish + bearish
        score = bullish / total if total > 0 else 0.5
        
        if score >= 0.65:
            signal = 'BUY'
        elif score <= 0.35:
            signal = 'SELL'
        else:
            signal = 'HOLD'
        
        reasoning = (
            f"price={price:.4f}, SMA20={float(sma_20.iloc[-1]):.4f}, "
            f"momentum={momentum:.2f}%, vol_trend={vol_trend:.2f}x, vola={volatility:.2f}%"
        )
        
        return {
            'score': round(score, 4),
            'signal': signal,
            'reasoning': reasoning,
            'block': 'fundamental',
            'details': {
                'momentum': momentum,
                'volatility': volatility,
                'vol_trend': vol_trend,
            }
        }


# === Block 3: Pattern Recognition ===

class PatternRecognitionAgent:
    """Блок 3: Распознавание свечных паттернов"""
    
    def __init__(self, config: Config, logger: logging.Logger):
        self.log = logger
        self.config = config
    
    def analyze(self, df: pd.DataFrame, symbol: str) -> Dict[str, Any]:
        if df.empty or len(df) < 10:
            return {'score': 0.5, 'signal': 'HOLD', 'reasoning': 'Insufficient data', 'block': 'pattern'}
        
        # Last N candles
        n = min(5, len(df))
        recent = df.iloc[-n:]
        
        close = recent['close']
        high = recent['high']
        low = recent['low']
        open_prices = recent['open']
        
        bullish_patterns = 0
        bearish_patterns = 0
        pattern_names = []
        
        for i in range(-1, -n, -1):
            body = abs(close.iloc[i] - open_prices.iloc[i])
            upper_wick = high.iloc[i] - max(close.iloc[i], open_prices.iloc[i])
            lower_wick = min(close.iloc[i], open_prices.iloc[i]) - low.iloc[i]
            total_range = high.iloc[i] - low.iloc[i]
            
            if total_range == 0:
                continue
            
            body_ratio = body / total_range
            
            # Doji
            if body_ratio < 0.1:
                pattern_names.append('doji')
            
            # Hammer / Shooting Star
            if body_ratio < 0.3:
                if lower_wick > body * 2 and upper_wick < body * 0.5:
                    bullish_patterns += 1
                    pattern_names.append('hammer')
                elif upper_wick > body * 2 and lower_wick < body * 0.5:
                    bearish_patterns += 1
                    pattern_names.append('shooting_star')
            
            # Engulfing
            if i < -1 and n >= 2:
                prev_body = abs(close.iloc[i+1] - open_prices.iloc[i+1])
                if prev_body > 0:
                    if close.iloc[i] > open_prices.iloc[i] and close.iloc[i+1] < open_prices.iloc[i+1]:
                        if close.iloc[i] > open_prices.iloc[i+1] and open_prices.iloc[i] < close.iloc[i+1]:
                            bullish_patterns += 2
                            pattern_names.append('bullish_engulfing')
                    elif close.iloc[i] < open_prices.iloc[i] and close.iloc[i+1] > open_prices.iloc[i+1]:
                        if close.iloc[i] < open_prices.iloc[i+1] and open_prices.iloc[i] > close.iloc[i+1]:
                            bearish_patterns += 2
                            pattern_names.append('bearish_engulfing')
            
            # Three white soldiers / three black crows
            if i < -2 and n >= 3:
                pass  # simplified
        
        # Price action: consecutive up/down closes
        consecutive = 0
        direction = 0
        for i in range(-1, -n, -1):
            if close.iloc[i] > close.iloc[i-1]:
                if direction == 1:
                    consecutive += 1
                else:
                    direction = 1
                    consecutive = 1
            elif close.iloc[i] < close.iloc[i-1]:
                if direction == -1:
                    consecutive += 1
                else:
                    direction = -1
                    consecutive = 1
        
        if consecutive >= 3:
            if direction == 1:
                bullish_patterns += 2
                pattern_names.append(f'3_up_candles')
            else:
                bearish_patterns += 2
                pattern_names.append(f'3_down_candles')
        
        total = bullish_patterns + bearish_patterns
        if total > 0:
            score = bullish_patterns / total
        else:
            score = 0.5
        
        if score >= 0.6:
            signal = 'BUY'
        elif score <= 0.4:
            signal = 'SELL'
        else:
            signal = 'HOLD'
        
        reasoning = f"patterns={', '.join(pattern_names[-5:]) or 'none'}, score={score:.2f}"
        
        return {
            'score': round(score, 4),
            'signal': signal,
            'reasoning': reasoning,
            'block': 'pattern',
            'details': {
                'bullish_count': bullish_patterns,
                'bearish_count': bearish_patterns,
                'patterns': pattern_names[-5:],
            }
        }


# === Block 4: News Analysis ===

class NewsAnalysisAgent:
    """Блок 4: Анализ новостного фона (заглушка — позже подключим реальный источник)"""
    
    def __init__(self, config: Config, logger: logging.Logger):
        self.log = logger
        self.config = config
    
    def analyze(self, symbol: str, sentiment_data: Dict = None) -> Dict[str, Any]:
        # Используем данные из SentimentAgent
        if sentiment_data is None:
            return {'score': 0.5, 'signal': 'HOLD', 'reasoning': 'No sentiment data', 'block': 'news'}
        
        avg_sent = sentiment_data.get('avg_sentiment', 0)
        news_count = sentiment_data.get('news_count', 0)
        bullish_ratio = sentiment_data.get('bullish_ratio', 0.5)
        
        # Normalize sentiment (-1..+1) to (0..1)
        score = (avg_sent + 1) / 2
        
        # Adjust by news count (more news = more confident)
        if news_count == 0:
            score = 0.5
        elif news_count > 10:
            score = min(1.0, score + 0.1)
        
        if score >= 0.6:
            signal = 'BUY'
        elif score <= 0.4:
            signal = 'SELL'
        else:
            signal = 'HOLD'
        
        reasoning = f"sentiment={avg_sent:.2f}, bullish_ratio={bullish_ratio:.0%}, news_count={news_count}"
        
        return {
            'score': round(score, 4),
            'signal': signal,
            'reasoning': reasoning,
            'block': 'news',
            'details': sentiment_data
        }


# === Block 5: Market Sentiment ===

class MarketSentimentAgent:
    """Блок 5: Анализ общих настроений рынка"""
    
    def __init__(self, config: Config, logger: logging.Logger):
        self.log = logger
        self.config = config
    
    def analyze(self, df: pd.DataFrame, symbol: str = None) -> Dict[str, Any]:
        if df.empty or len(df) < 20:
            return {'score': 0.5, 'signal': 'HOLD', 'reasoning': 'Insufficient data', 'block': 'market_sentiment'}
        
        close = df['close']
        volume = df['volume']
        
        # Fear/greed estimation from volatility and volume
        returns = close.pct_change().dropna()
        volatility = float(returns.std())
        
        # High vol = fear, low vol = greed
        if volatility > 0.05:
            vol_score = 0.3  # high fear
        elif volatility < 0.01:
            vol_score = 0.7  # greed
        else:
            vol_score = 0.5
        
        # Volume surge = potential reversal or continuation
        vol_sma = volume.rolling(20).mean()
        vol_ratio = float(volume.iloc[-1] / vol_sma.iloc[-1]) if vol_sma.iloc[-1] > 0 else 1.0
        
        if vol_ratio > 2.0:
            vol_score = min(1.0, vol_score + 0.2)  # high volume, market active
        
        # Recent momentum
        if len(close) >= 5:
            momentum_5 = float(close.iloc[-1] / close.iloc[-5] - 1)
        else:
            momentum_5 = 0
        
        if momentum_5 > 0.03:
            mom_score = 0.7
        elif momentum_5 < -0.03:
            mom_score = 0.3
        else:
            mom_score = 0.5
        
        score = (vol_score + mom_score) / 2
        
        if score >= 0.65:
            signal = 'BUY'
        elif score <= 0.35:
            signal = 'SELL'
        else:
            signal = 'HOLD'
        
        reasoning = f"vol={volatility:.4f}, vol_ratio={vol_ratio:.2f}x, mom_5={momentum_5:.2%}"
        
        return {
            'score': round(score, 4),
            'signal': signal,
            'reasoning': reasoning,
            'block': 'market_sentiment',
            'details': {
                'volatility': volatility,
                'vol_ratio': vol_ratio,
                'momentum_5': momentum_5,
            }
        }


# === Block 6: Pump Hunter ===

class PumpHunterAgent:
    """Блок 6: Стратегия Pump Hunter — поиск резких движений и вход на откатах"""
    
    def __init__(self, config: Config, logger: logging.Logger):
        self.log = logger
        self.config = config
    
    def analyze(self, df: pd.DataFrame, symbol: str) -> Dict[str, Any]:
        if df.empty or len(df) < 20:
            return {'score': 0.5, 'signal': 'HOLD', 'reasoning': 'Insufficient data', 'block': 'pump_hunter'}
        
        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']
        
        price = float(close.iloc[-1])
        
        # Detect pumps (sharp moves up)
        pump_threshold = 0.03  # 3% in one candle
        drop_threshold = 0.02  # 2% drop after pump
        
        pump_detected = False
        in_retrace = False
        retrace_pct = 0.0
        
        for i in range(-5, -1):
            move = (close.iloc[i] - close.iloc[i-1]) / close.iloc[i-1]
            if move > pump_threshold:
                pump_detected = True
                # Check if we're in retrace
                retrace_pct = (close.iloc[-1] - close.iloc[i]) / close.iloc[i]
                if retrace_pct < 0 and retrace_pct > -0.10:
                    in_retrace = True
        
        # CSS for trend
        ma_period = self.config.css_indicator.get('sma_period', 20)
        ma = close.rolling(ma_period).mean()
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        slope = (ma - ma.shift(1)) / atr.iloc[-1] if atr.iloc[-1] > 0 else 0
        css = float(slope.iloc[-1])
        
        # Volume surge detection
        vol_sma = volume.rolling(20).mean()
        vol_ratio = float(volume.iloc[-1] / vol_sma.iloc[-1]) if vol_sma.iloc[-1] > 0 else 1.0
        
        score = 0.5
        
        if pump_detected and in_retrace:
            # Ideal pump hunter entry: after pump, in small retrace
            score = 0.75
            if css > 0.1:
                score = 0.85
        elif pump_detected:
            # Pump but no retrace yet, wait
            score = 0.55
        elif vol_ratio > 2.0 and css > 0.1:
            # Volume surge + positive trend = potential pump setup
            score = 0.65
        elif vol_ratio > 1.5 and css < -0.1:
            # Volume surge + negative trend = potential dump
            score = 0.30
        
        if score >= 0.65:
            signal = 'BUY'
        elif score <= 0.35:
            signal = 'SELL'
        else:
            signal = 'HOLD'
        
        reasoning = (
            f"pump={pump_detected}, retrace={in_retrace} ({retrace_pct:.2%}), "
            f"css={css:.4f}, vol_ratio={vol_ratio:.2f}x"
        )
        
        return {
            'score': round(score, 4),
            'signal': signal,
            'reasoning': reasoning,
            'block': 'pump_hunter',
            'details': {
                'pump_detected': pump_detected,
                'in_retrace': in_retrace,
                'retrace_pct': retrace_pct,
                'css': css,
                'vol_ratio': vol_ratio,
            }
        }


# === AGGREGATOR ===

class DecisionAggregator:
    """Aggregates scores from all 6 blocks into final decision"""
    
    # Weights for each block
    WEIGHTS = {
        'technical': 0.25,
        'fundamental': 0.15,
        'pattern': 0.20,
        'news': 0.15,
        'market_sentiment': 0.10,
        'pump_hunter': 0.15,
    }
    
    def __init__(self, logger: logging.Logger):
        self.log = logger
    
    def aggregate(self, results: Dict[str, Dict[str, Any]], min_confidence: float = 0.35) -> Dict[str, Any]:
        """
        Compute weighted average of all block scores.
        results: {block_name: {score, signal, reasoning, details}}
        """
        weighted_sum = 0.0
        total_weight = 0.0
        
        scores_by_signal = {'BUY': 0.0, 'SELL': 0.0, 'HOLD': 0.0}
        weights_by_signal = {'BUY': 0.0, 'SELL': 0.0, 'HOLD': 0.0}
        
        for block_name, result in results.items():
            weight = self.WEIGHTS.get(block_name, 0.0)
            score = result.get('score', 0.5)
            signal = result.get('signal', 'HOLD')
            
            weighted_sum += score * weight
            total_weight += weight
            
            # Track by signal for additional logic
            scores_by_signal[signal] += score * weight
            weights_by_signal[signal] += weight
        
        # Normalize
        final_score = weighted_sum / total_weight if total_weight > 0 else 0.5
        
        # Override: if weighted score conflicts with majority signal, apply rules
        if weights_by_signal['BUY'] >= 0.5 and final_score >= 0.55:
            final_signal = 'BUY'
        elif weights_by_signal['SELL'] >= 0.5 and final_score <= 0.45:
            final_signal = 'SELL'
        elif final_score >= 0.60:
            final_signal = 'BUY'
        elif final_score <= 0.40:
            final_signal = 'SELL'
        else:
            final_signal = 'HOLD'
        
        # Confidence = normalized weighted score distance from 0.5
        confidence = abs(final_score - 0.5) * 2
        
        # If confidence too low, force HOLD
        if confidence < min_confidence:
            final_signal = 'HOLD'
            confidence = min_confidence
        
        self.log.info(
            f"Aggregator: final_score={final_score:.3f}, confidence={confidence:.3f}, "
            f"signal={final_signal}, blocks={list(results.keys())}"
        )
        
        # Build reasoning
        block_reasons = [f"{r['block']}: {r['signal']}({r['score']:.2f})" for r in results.values()]
        
        return {
            'signal': final_signal,
            'confidence': round(confidence, 4),
            'score': round(final_score, 4),
            'reasoning': f"Aggregated: {', '.join(block_reasons)}",
            'blocks': results,
            'weights': self.WEIGHTS,
        }


# === MAIN MULTI-AGENT ENGINE ===

class MultiAgentDecisionEngine:
    """
    Main orchestrator: runs all 6 blocks in parallel,
    aggregates results, returns final decision.
    """
    
    def __init__(self, config: Config, logger: logging.Logger):
        self.log = logger
        self.config = config
        
        # Initialize all 6 blocks
        self.blocks = {
            'technical': TechnicalAnalysisAgent(config, logger),
            'fundamental': FundamentalAnalysisAgent(config, logger),
            'pattern': PatternRecognitionAgent(config, logger),
            'news': NewsAnalysisAgent(config, logger),
            'market_sentiment': MarketSentimentAgent(config, logger),
            'pump_hunter': PumpHunterAgent(config, logger),
        }
        
        self.aggregator = DecisionAggregator(logger)
        self.min_confidence = config.agents.get('trading_decision', {}).get('min_confidence', 0.35)
    
    def analyze(self, df: pd.DataFrame, symbol: str,
                sentiment_data: Dict = None) -> Dict[str, Any]:
        """
        Run all 6 blocks and return aggregated decision.
        
        Args:
            df: OHLCV DataFrame with columns [timestamp, open, high, low, close, volume]
            symbol: trading symbol
            sentiment_data: optional dict with keys avg_sentiment, bullish_ratio, news_count
        
        Returns:
            Aggregated decision with all block results
        """
        results = {}
        
        # Run blocks that need df
        try:
            results['technical'] = self.blocks['technical'].analyze(df, symbol)
        except Exception as e:
            self.log.error(f"Technical block error: {e}")
            results['technical'] = {'score': 0.5, 'signal': 'HOLD', 'reasoning': str(e), 'block': 'technical'}
        
        try:
            results['fundamental'] = self.blocks['fundamental'].analyze(df, symbol)
        except Exception as e:
            self.log.error(f"Fundamental block error: {e}")
            results['fundamental'] = {'score': 0.5, 'signal': 'HOLD', 'reasoning': str(e), 'block': 'fundamental'}
        
        try:
            results['pattern'] = self.blocks['pattern'].analyze(df, symbol)
        except Exception as e:
            self.log.error(f"Pattern block error: {e}")
            results['pattern'] = {'score': 0.5, 'signal': 'HOLD', 'reasoning': str(e), 'block': 'pattern'}
        
        try:
            results['market_sentiment'] = self.blocks['market_sentiment'].analyze(df, symbol)
        except Exception as e:
            self.log.error(f"Market sentiment block error: {e}")
            results['market_sentiment'] = {'score': 0.5, 'signal': 'HOLD', 'reasoning': str(e), 'block': 'market_sentiment'}
        
        try:
            results['pump_hunter'] = self.blocks['pump_hunter'].analyze(df, symbol)
        except Exception as e:
            self.log.error(f"PumpHunter block error: {e}")
            results['pump_hunter'] = {'score': 0.5, 'signal': 'HOLD', 'reasoning': str(e), 'block': 'pump_hunter'}
        
        # News block uses sentiment data
        try:
            results['news'] = self.blocks['news'].analyze(symbol, sentiment_data)
        except Exception as e:
            self.log.error(f"News block error: {e}")
            results['news'] = {'score': 0.5, 'signal': 'HOLD', 'reasoning': str(e), 'block': 'news'}
        
        # Aggregate
        decision = self.aggregator.aggregate(results, self.min_confidence)
        decision['symbol'] = symbol
        decision['timestamp'] = datetime.utcnow().isoformat()
        
        return decision
