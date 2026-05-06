import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple


# =============================================================================
# ORDER BLOCK + BREAK OF STRUCTURE INDICATORS
# Based on MQL5 "Inducement Mitigation Block" strategy
# =============================================================================

def detect_swing_highs_lows(df: pd.DataFrame, lookback: int = 5) -> Tuple[pd.Series, pd.Series]:
    """
    Find swing highs and swing lows using local max/min detection.
    lookback: how many bars on each side must be lower/higher
    Returns: (swing_highs, swing_lows) as boolean Series
    """
    high = df['high']
    low = df['low']
    
    swing_highs = pd.Series(False, index=df.index)
    swing_lows = pd.Series(False, index=df.index)
    
    for i in range(lookback, len(df) - lookback):
        # Swing high: current bar is highest in window
        if high.iloc[i] == high.iloc[i-lookback:i+lookback+1].max():
            swing_highs.iloc[i] = True
        # Swing low: current bar is lowest in window
        if low.iloc[i] == low.iloc[i-lookback:i+lookback+1].min():
            swing_lows.iloc[i] = True
    
    return swing_highs, swing_lows


def detect_order_blocks(df: pd.DataFrame, lookback: int = 50, 
                       impulse_min_size: float = 0.005) -> List[Dict]:
    """
    Detect Order Blocks — last opposing candle before strong impulsive move.
    
    Bullish OB: last bearish candle BEFORE a strong bullish impulse
    Bearish OB: last bullish candle BEFORE a strong bearish impulse
    
    Returns list of dicts with: {type, index, high, low, quality, impulsive_index}
    quality = 1-3 based on size of following impulse
    """
    close = df['close']
    high = df['high']
    low = df['low']
    open_ = df['open']
    
    order_blocks = []
    atr = calculate_atr(df, period=14).iloc[-1] if len(df) > 14 else (close.max() - close.min()) / len(df)
    
    for i in range(lookback, len(df) - 3):
        current_bar_return = (close.iloc[i] - open_.iloc[i]) / open_.iloc[i]
        # Look at next 1-3 bars as impulse
        future_returns = [(close.iloc[i+j] - close.iloc[i]) / close.iloc[i] for j in range(1, 4)]
        max_impulse = max(future_returns) if future_returns else 0
        
        # Bullish OB: current is bearish, next bars rally
        if current_bar_return < -0.001:  # bearish candle (>0.1% down)
            if max_impulse >= impulse_min_size:  # strong follow-through
                # Check if this is the LAST bearish candle before impulse
                prev_bars_are_bearish = all(
                    (close.iloc[i-k] - open_.iloc[i-k]) / open_.iloc[i-k] < 0 
                    for k in range(1, 3)
                )
                next_bar_bullish = future_returns[0] > 0.001
                
                if prev_bars_are_bearish and next_bar_bullish:
                    quality = 1 + int(max_impulse / 0.01) + int(max_impulse / 0.02)
                    quality = min(quality, 3)
                    
                    order_blocks.append({
                        'type': 'BULLISH',
                        'index': i,
                        'high': float(high.iloc[i]),
                        'low': float(low.iloc[i]),
                        'close': float(close.iloc[i]),
                        'open': float(open_.iloc[i]),
                        'quality': quality,
                        'impulse_size': max_impulse,
                        'atr_factor': max_impulse * close.iloc[i] / atr if atr > 0 else 0,
                    })
        
        # Bearish OB: current is bullish, next bars drop
        elif current_bar_return > 0.001:  # bullish candle (>0.1% up)
            if min(future_returns) <= -impulse_min_size:  # strong follow-through down
                prev_bars_are_bullish = all(
                    (close.iloc[i-k] - open_.iloc[i-k]) / open_.iloc[i-k] > 0 
                    for k in range(1, 3)
                )
                next_bar_bearish = future_returns[0] < -0.001
                
                if prev_bars_are_bullish and next_bar_bearish:
                    quality = 1 + int(abs(min(future_returns)) / 0.01) + int(abs(min(future_returns)) / 0.02)
                    quality = min(quality, 3)
                    
                    order_blocks.append({
                        'type': 'BEARISH', 
                        'index': i,
                        'high': float(high.iloc[i]),
                        'low': float(low.iloc[i]),
                        'close': float(close.iloc[i]),
                        'open': float(open_.iloc[i]),
                        'quality': quality,
                        'impulse_size': abs(min(future_returns)),
                        'atr_factor': abs(min(future_returns)) * close.iloc[i] / atr if atr > 0 else 0,
                    })
    
    return order_blocks


def detect_break_of_structure(df: pd.DataFrame, lookback: int = 50) -> Dict:
    """
    Detect Break of Structure (BoS) — price breaks above swing high or below swing low.
    Returns: {has_bullish_bos, has_bearish_bos, bos_index, bos_price, last_swing_high, last_swing_low}
    """
    # Reset to positional index for clean integer access
    df = df.reset_index(drop=True)
    close = df['close']
    
    swing_highs, swing_lows = detect_swing_highs_lows(df, lookback=5)
    
    # Find last swing high/low indices (positional)
    swing_high_indices = swing_highs[swing_highs].index.tolist()
    swing_low_indices = swing_lows[swing_lows].index.tolist()
    
    last_sh_idx = swing_high_indices[-1] if swing_high_indices else None
    last_sl_idx = swing_low_indices[-1] if swing_low_indices else None
    
    last_sh_price = float(df['high'].iloc[last_sh_idx]) if last_sh_idx is not None else None
    last_sl_price = float(df['low'].iloc[last_sl_idx]) if last_sl_idx is not None else None
    
    current_price = float(close.iloc[-1])
    current_idx = len(df) - 1
    
    result = {
        'last_swing_high_idx': int(last_sh_idx) if last_sh_idx is not None else None,
        'last_swing_low_idx': int(last_sl_idx) if last_sl_idx is not None else None,
        'last_swing_high_price': last_sh_price,
        'last_swing_low_price': last_sl_price,
        'current_price': current_price,
        'has_bullish_bos': False,
        'has_bearish_bos': False,
        'bos_type': None,
        'bars_since_bos': None,
    }
    
    if last_sh_idx is not None and last_sh_idx < current_idx:
        sh_idx = int(last_sh_idx)
        bars_since = current_idx - sh_idx
        if bars_since <= lookback:
            # Has price been consistently above last swing high?
            above_sh = all(df['close'].iloc[j] > last_sh_price
                          for j in range(sh_idx + 1, current_idx + 1))
            if above_sh and current_price > last_sh_price:
                result['has_bullish_bos'] = True
                result['bos_type'] = 'BULLISH'
                result['bars_since_bos'] = bars_since
    
    if last_sl_idx is not None and last_sl_idx < current_idx:
        sl_idx = int(last_sl_idx)
        bars_since = current_idx - sl_idx
        if bars_since <= lookback:
            below_sl = all(df['close'].iloc[j] < last_sl_price
                          for j in range(sl_idx + 1, current_idx + 1))
            if below_sl and current_price < last_sl_price:
                result['has_bearish_bos'] = True
                result['bos_type'] = 'BEARISH'
                result['bars_since_bos'] = bars_since
    
    return result


def detect_inducement(df: pd.DataFrame, order_blocks: List[Dict], 
                     lookback: int = 10) -> List[Dict]:
    """
    Detect Inducement — small counter-trend move BEFORE break of structure.
    Validates order blocks by checking if there's a false break before real BoS.
    
    Inducement = small move AGAINST the eventual direction, right before the break.
    Separates genuine setups from noise.
    """
    if not order_blocks:
        return []
    
    close = df['close']
    
    validated_obs = []
    for ob in order_blocks:
        ob_idx = ob['index']
        ob_type = ob['type']
        
        # Look for inducement AFTER the order block, before current bar
        if ob_idx + lookback >= len(df):
            continue
        
        # Inducement for bullish OB = small drop before rally
        # Inducement for bearish OB = small rise before drop
        if ob_type == 'BULLISH':
            # Check 3-8 bars after OB for small counter-move
            for offset in range(3, min(lookback, len(df) - ob_idx - 1)):
                test_idx = ob_idx + offset
                if test_idx >= len(df):
                    break
                # Inducement: price dips slightly then recovers
                ob_low = ob['low']
                test_low = float(df['low'].iloc[test_idx])
                dip_pct = (ob_low - test_low) / ob_low if ob_low > 0 else 0
                
                if dip_pct > 0.002 and dip_pct < 0.015:  # 0.2%-1.5% dip = inducement
                    validated_obs.append({**ob, 'has_inducement': True, 'inducement_depth': dip_pct})
                    break
            else:
                # No inducement found — still valid but lower quality
                validated_obs.append({**ob, 'has_inducement': False, 'inducement_depth': 0})
        
        elif ob_type == 'BEARISH':
            for offset in range(3, min(lookback, len(df) - ob_idx - 1)):
                test_idx = ob_idx + offset
                if test_idx >= len(df):
                    break
                ob_high = ob['high']
                test_high = float(df['high'].iloc[test_idx])
                rise_pct = (test_high - ob_high) / ob_high if ob_high > 0 else 0
                
                if rise_pct > 0.002 and rise_pct < 0.015:  # 0.2%-1.5% rise = inducement
                    validated_obs.append({**ob, 'has_inducement': True, 'inducement_depth': rise_pct})
                    break
            else:
                validated_obs.append({**ob, 'has_inducement': False, 'inducement_depth': 0})
    
    return validated_obs


def detect_fair_value_gaps(df: pd.DataFrame, lookback: int = 20) -> List[Dict]:
    """
    Detect Fair Value Gaps (FVG) — gaps between 2nd and 3rd candle in impulse.
    Formed when: candle 2 body doesn't overlap with candle 1 body, and candle 3 
    doesn't overlap with candle 1.
    
    Bullish FVG: gap between candle 1 high and candle 3 low
    Bearish FVG: gap between candle 3 high and candle 1 low
    """
    gaps = []
    close = df['close']
    open_ = df['open']
    high = df['high']
    low = df['low']
    
    for i in range(2, len(df)):
        c1_high = float(high.iloc[i-2])
        c1_low = float(low.iloc[i-2])
        c1_open = float(open_.iloc[i-2])
        c1_close = float(close.iloc[i-2])
        
        c2_high = float(high.iloc[i-1])
        c2_low = float(low.iloc[i-1])
        
        c3_high = float(high.iloc[i])
        c3_low = float(low.iloc[i])
        c3_open = float(open_.iloc[i])
        c3_close = float(close.iloc[i])
        
        # Bullish FVG: 2nd candle gaps up from 1st (c2_low > c1_high)
        if c2_low > c1_high:
            gaps.append({
                'type': 'BULLISH',
                'index': i-1,
                'gap_top': c2_low,
                'gap_bottom': c1_high,
                'size': c2_low - c1_high,
                'candle_index': i-1,
            })
        
        # Bearish FVG: 2nd candle gaps down from 1st (c2_high < c1_low)  
        elif c2_high < c1_low:
            gaps.append({
                'type': 'BEARISH',
                'index': i-1,
                'gap_top': c1_low,
                'gap_bottom': c2_high,
                'size': c1_low - c2_high,
                'candle_index': i-1,
            })
    
    # Return last N gaps
    return gaps[-lookback:] if len(gaps) > lookback else gaps


def detect_mitigation(df: pd.DataFrame, order_blocks: List[Dict]) -> List[Dict]:
    """
    Check if order blocks have been mitigated — price passed through them.
    Mitigation % = how deep price went into the OB zone.
    
    <20% mitigation = fresh OB (high quality)
    20-60% mitigation = partial (medium quality)
    >60% mitigation = deeply mitigated (low quality, avoid)
    """
    close = df['close']
    
    for ob in order_blocks:
        ob_high = ob['high']
        ob_low = ob['low']
        ob_type = ob['type']
        
        # Find all bars AFTER the order block
        ob_idx = ob['index']
        post_ob_bars = df.iloc[ob_idx+1:]
        
        if post_ob_bars.empty:
            ob['mitigation_pct'] = 0
            ob['mitigation_status'] = 'FRESH'
            continue
        
        if ob_type == 'BULLISH':
            # How deep did price go below the OB low?
            lowest_post = post_ob_bars['low'].min()
            ob_range = ob_high - ob_low
            if ob_range > 0:
                depth_below = max(0, ob_low - lowest_post)
                ob['mitigation_pct'] = min(depth_below / ob_range * 100, 150)
            else:
                ob['mitigation_pct'] = 0
                
        elif ob_type == 'BEARISH':
            highest_post = post_ob_bars['high'].max()
            ob_range = ob_high - ob_low
            if ob_range > 0:
                depth_above = max(0, highest_post - ob_high)
                ob['mitigation_pct'] = min(depth_above / ob_range * 100, 150)
            else:
                ob['mitigation_pct'] = 0
        
        # Classify
        mp = ob['mitigation_pct']
        if mp < 20:
            ob['mitigation_status'] = 'FRESH'
        elif mp < 60:
            ob['mitigation_status'] = 'PARTIAL'
        else:
            ob['mitigation_status'] = 'DEEPLY_MITIGATED'
    
    return order_blocks


def calculate_round_number_zones(price: float, atr: float, 
                                  timeframes: List[str] = None) -> Dict:
    """
    Calculate round number (psychological) levels and their ZeroSize.
    ZeroSize = number of trailing zeros after significant digits.
    
    Examples:
    - $70,000 → ZeroSize 4 (macro level)
    - $70,500 → ZeroSize 2 (minor)
    - $71,000 → ZeroSize 3
    
    Also returns distance to nearest round levels in ATR units.
    """
    if timeframes is None:
        timeframes = ['M15', 'H4', 'D1']
    
    # Timeframe -> min ZeroSize
    tf_min_zs = {'M15': 2, 'H4': 3, 'D1': 4}
    
    # Find nearest round numbers
    def zeros_in_price(p: float) -> int:
        """Count trailing zeros in price string representation"""
        price_str = f"{p:.4f}".rstrip('0')
        zeros = 0
        for ch in reversed(price_str):
            if ch == '0':
                zeros += 1
            elif ch == '.':
                continue
            else:
                break
        return zeros
    
    zs = zeros_in_price(price)
    
    # Distance to nearest round levels (above and below)
    magnitude = 10 ** zs if zs > 0 else 1
    nearest_below = (price // magnitude) * magnitude
    nearest_above = nearest_below + magnitude
    
    dist_below = price - nearest_below
    dist_above = nearest_above - price
    
    # ATR distance
    dist_below_atr = dist_below / atr if atr > 0 else 0
    dist_above_atr = dist_above / atr if atr > 0 else 0
    
    return {
        'price': price,
        'zerosize': zs,
        'nearest_round_below': nearest_below,
        'nearest_round_above': nearest_above,
        'dist_to_round_below': dist_below,
        'dist_to_round_above': dist_above,
        'dist_below_atr': dist_below_atr,
        'dist_above_atr': dist_above_atr,
        'atr': atr,
        'nearest_round_dist_pct': min(dist_below, dist_above) / price * 100 if price > 0 else 0,
    }


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range"""
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def analyze_ob_structure(df: pd.DataFrame, lookback: int = 50) -> Dict:
    """
    Master function: combines all OB+BoS+Inducement analysis.
    Returns complete structure analysis for trading decisions.
    """
    if len(df) < lookback + 10:
        return {
            'has_bullish_setup': False,
            'has_bearish_setup': False,
            'order_blocks': [],
            'bos': {},
            'gaps': [],
            'nearest_round': {},
            'signal': 'HOLD',
            'confidence': 0.0,
            'reasoning': 'Insufficient data',
        }
    
    atr = calculate_atr(df).iloc[-1]
    current_price = float(df['close'].iloc[-1])
    
    # 1. Detect order blocks
    obs = detect_order_blocks(df, lookback=lookback)
    
    # 2. Add inducement filter
    obs = detect_inducement(df, obs)
    
    # 3. Add mitigation check
    obs = detect_mitigation(df, obs)
    
    # 4. Break of Structure
    bos = detect_break_of_structure(df, lookback=lookback)
    
    # 5. Fair Value Gaps
    gaps = detect_fair_value_gaps(df, lookback=20)
    
    # 6. Round number analysis
    round_info = calculate_round_number_zones(current_price, atr)
    
    # --- Evaluate Bullish Setup ---
    # Valid bullish OB: bullish type, quality>=1 (relaxed), inducement optional but preferred
    # Mitigation < 80% (deeply mitigated only excluded)
    valid_bull_obs = [ob for ob in obs 
                      if ob['type'] == 'BULLISH' 
                      and ob['quality'] >= 1
                      and ob.get('mitigation_status', 'FRESH') != 'DEEPLY_MITIGATED']
    
    # Also include OBs with inducement (higher quality signal)
    inducement_bull = [ob for ob in valid_bull_obs if ob.get('has_inducement', False)]
    no_inducement_bull = [ob for ob in valid_bull_obs if not ob.get('has_inducement', False)]
    
    # Check if bullish BoS occurred
    bullish_bos = bos.get('has_bullish_bos', False)
    
    # Price currently above last swing high = bullish structure
    above_last_sh = (bos.get('last_swing_high_price') and 
                      current_price > bos['last_swing_high_price'])
    
    # Price retraced TO a valid OB zone = entry opportunity
    price_at_bull_ob = False
    for ob in valid_bull_obs:
        if ob['low'] <= current_price <= ob['high'] * 1.01:
            price_at_bull_ob = True
            break
    
    # Primary: BoS confirmed + price at/near OB zone
    # Secondary: just price at fresh OB zone with bullish structure
    bullish_setup = (len(valid_bull_obs) > 0 and 
                     (bullish_bos or above_last_sh) and 
                     (price_at_bull_ob or len(inducement_bull) > 0))
    
    # --- Evaluate Bearish Setup ---
    valid_bear_obs = [ob for ob in obs 
                      if ob['type'] == 'BEARISH' 
                      and ob['quality'] >= 1
                      and ob.get('mitigation_status', 'FRESH') != 'DEEPLY_MITIGATED']
    
    inducement_bear = [ob for ob in valid_bear_obs if ob.get('has_inducement', False)]
    no_inducement_bear = [ob for ob in valid_bear_obs if not ob.get('has_inducement', False)]
    
    bearish_bos = bos.get('has_bearish_bos', False)
    below_last_sl = (bos.get('last_swing_low_price') and 
                      current_price < bos['last_swing_low_price'])
    
    price_at_bear_ob = False
    for ob in valid_bear_obs:
        if ob['high'] >= current_price >= ob['low'] * 0.99:
            price_at_bear_ob = True
            break
    
    bearish_setup = (len(valid_bear_obs) > 0 and 
                     (bearish_bos or below_last_sl) and 
                     (price_at_bear_ob or len(inducement_bear) > 0))
    
    # --- Round number confluence ---
    near_round_pct = round_info['nearest_round_dist_pct']
    near_round_confluence = near_round_pct < 1.0  # within 1% of round level = confluence
    
    # Quality of nearest OB
    best_bull_q = max([ob['quality'] for ob in valid_bull_obs], default=0)
    best_bear_q = max([ob['quality'] for ob in valid_bear_obs], default=0)
    bull_inducement_count = len(inducement_bull)
    bear_inducement_count = len(inducement_bear)
    
    # --- Build signal ---
    signal = 'HOLD'
    confidence = 0.0
    reasoning = []
    
    if bullish_setup and not bearish_setup:
        signal = 'BUY'
        # Base confidence from quality + inducement + round level
        base_conf = 0.50
        quality_bonus = best_bull_q * 0.08  # 0.08 per quality level
        inducement_bonus = bull_inducement_count * 0.05  # 0.05 per OB with inducement
        round_bonus = 0.08 if near_round_confluence else 0
        fvg_bonus = 0.05 if gaps and any(g['type'] == 'BULLISH' for g in gaps) else 0
        bos_bonus = 0.05 if bullish_bos else 0
        confidence = min(base_conf + quality_bonus + inducement_bonus + round_bonus + fvg_bonus + bos_bonus, 0.92)
        conf_parts = [
            f"+{len(valid_bull_obs)} bull OB(s) q={best_bull_q}/3",
            f"{bull_inducement_count} with inducement",
            f"BoS={'YES' if bullish_bos else 'struct above' if above_last_sh else 'weak'}",
        ]
        if near_round_confluence:
            conf_parts.append(f"round ZS{round_info.get('zerosize', 0)}")
        if gaps and any(g['type'] == 'BULLISH' for g in gaps):
            conf_parts.append("FVG")
        reasoning = " | ".join(conf_parts)
    
    elif bearish_setup and not bullish_setup:
        signal = 'SELL'
        base_conf = 0.50
        quality_bonus = best_bear_q * 0.08
        inducement_bonus = bear_inducement_count * 0.05
        round_bonus = 0.08 if near_round_confluence else 0
        fvg_bonus = 0.05 if gaps and any(g['type'] == 'BEARISH' for g in gaps) else 0
        bos_bonus = 0.05 if bearish_bos else 0
        confidence = min(base_conf + quality_bonus + inducement_bonus + round_bonus + fvg_bonus + bos_bonus, 0.92)
        conf_parts = [
            f"+{len(valid_bear_obs)} bear OB(s) q={best_bear_q}/3",
            f"{bear_inducement_count} with inducement",
            f"BoS={'YES' if bearish_bos else 'struct below' if below_last_sl else 'weak'}",
        ]
        if near_round_confluence:
            conf_parts.append(f"round ZS{round_info.get('zerosize', 0)}")
        if gaps and any(g['type'] == 'BEARISH' for g in gaps):
            conf_parts.append("FVG")
        reasoning = " | ".join(conf_parts)
    
    elif bullish_setup and bearish_setup:
        signal = 'HOLD'
        confidence = 0.45
        reasoning = f"CONFLICT: {len(valid_bull_obs)} bull OB vs {len(valid_bear_obs)} bear OB"
    
    else:
        signal = 'HOLD'
        if obs:
            reasoning = f"OBs found but conditions not met (need BoS + retracement)"
        else:
            reasoning = "No valid OB+BoS setups"
        confidence = 0.48
    
    return {
        'has_bullish_setup': bullish_setup,
        'has_bearish_setup': bearish_setup,
        'bullish_obs': valid_bull_obs,
        'bearish_obs': valid_bear_obs,
        'all_obs': obs,
        'bos': bos,
        'gaps': gaps,
        'nearest_round': round_info,
        'signal': signal,
        'confidence': confidence,
        'reasoning': reasoning,
        'current_price': current_price,
        'atr': atr,
    }


# =============================================================================
# STANDARD TECHNICAL INDICATORS
# =============================================================================

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
