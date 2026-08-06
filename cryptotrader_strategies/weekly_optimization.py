#!/usr/bin/env python3
"""
weekly_optimization.py — Еженедельная оптимизация universe пар.

ЗАЧЕМ: Каждое воскресенье автоматически:
1. Сканирует все Bybit linear USDT perpetuals
2. Фильтрует по ликвидности (24h volume > $1M)
3. Загружает OHLCV 90д для топ-50 пар
4. Тестирует V8 + Score на каждой
5. Выбирает прибыльные пары (test exp > 0, n >= 3)
6. Обновляет STRATEGIES["symbols"] в clone5_multi_runner.py
7. Сохраняет результаты в JSON

РЕЗУЛЬТАТ:
- universe.json — текущий список прибыльных пар
- weekly_optimization_YYYYMMDD.json — архивный отчёт
- clone5_multi_runner.py — обновлённый symbols list

ИСПОЛЬЗОВАНИЕ:
  python3 weekly_optimization.py
  
КРОН: Каждое воскресенье 00:00 MSK (cron job ID: TBD)
"""

import ccxt
import pickle
import json
import time
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
import sys

# Пути
CLAUDE_DATA = Path("/tmp/claude-1000/-home-andy/68c8f4a2-34c3-48fc-a169-59db22bef8f2/scratchpad")
RUNNER_PATH = Path("/home/andy/CryptoTrader_main/cryptotrader_strategies/clone5_multi_runner.py")
UNIVERSE_JSON = Path("/home/andy/CryptoTrader_main/config/universe.json")
REPORTS_DIR = Path("/home/andy/CryptoTrader_main/reports/weekly_optimization")

# V8 параметры
V8_BASE = {
    'swing_lookback': 50, 'sweep_threshold': 0.003, 'wick_body_min_ratio': 1.0,
    'min_volume_spike': 1.3, 'session_start_utc': 6, 'session_end_utc': 20,
    'trailing_step_pct': 0.30, 'max_hold_bars': 36, 'rr_ratio': 2.5,
    'choch_lookback': 10, 'choch_min_bars': 2,
}

SCORE_BONUSES = {'fvg': 0.10, 'order_block': 0.10, 'vsa_absorption': 0.15, 'htf_trend': 0.10, 'kill_zone': 0.05}
BASE_SCORE = 0.50
MIN_SCORE = 0.55
FEE_PCT = 0.11


# ═══════════════════════════════════════════════════════════════════════════════
# [Fix 03.08.2026 Claude] Безопасное обновление боевого STRATEGIES["symbols"].
#
# Было: re.sub(r'("symbols":\s*\[)([^\]]+)(\])', ...) + write_text() без проверки.
# Почему сломалось 02.08 (скан лежал 390 циклов, торговля стояла ~40 часов):
#   класс [^\]] обрывается на ПЕРВОЙ ']', а внутри блока symbols есть комментарий
#   «# [30.07.2026] Добавлены 4 пары» — regex съел только часть списка, хвост
#   старого остался за новой ']' → SyntaxError в боевом файле.
#
# Стало: границы списка берутся из AST (точные lineno/col_offset узла), результат
# валидируется ast.parse + сверкой прочитанного обратно списка, при любой ошибке —
# откат из бэкапа. Битый файл в боевой путь попасть не может.
# ═══════════════════════════════════════════════════════════════════════════════
def update_runner_symbols(new_symbols, runner_path=None):
    """Переписывает STRATEGIES[0]["symbols"] в runner-файле. Возвращает (ok, message)."""
    import ast
    import shutil

    path = Path(runner_path) if runner_path else RUNNER_PATH

    bad = [s for s in new_symbols if not s.endswith('USDT') or s.count('USDT') > 1]
    if bad:
        return False, f"отказ: невалидные имена пар {bad[:5]} — файл не тронут"
    if len(new_symbols) < 5:
        return False, f"отказ: подозрительно мало пар ({len(new_symbols)}) — файл не тронут"

    src = path.read_text()
    lines = src.splitlines(keepends=True)
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln))

    def abs_pos(lineno, col):
        return offsets[lineno - 1] + col

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, f"отказ: боевой файл УЖЕ содержит SyntaxError ({e}) — чинить вручную"

    node = None
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == 'STRATEGIES' for t in stmt.targets):
            continue
        if not (isinstance(stmt.value, ast.List) and stmt.value.elts):
            continue
        first = stmt.value.elts[0]
        if not isinstance(first, ast.Dict):
            continue
        for k, v in zip(first.keys, first.values):
            if isinstance(k, ast.Constant) and k.value == 'symbols':
                node = v
        break

    if node is None or not isinstance(node, ast.List):
        return False, "отказ: не найден литерал STRATEGIES[0]['symbols'] — файл не тронут"

    start = abs_pos(node.lineno, node.col_offset)
    end = abs_pos(node.end_lineno, node.end_col_offset)
    indent = ' ' * (node.col_offset)
    body = ',\n'.join(f'{indent}    "{s}"' for s in new_symbols)
    replacement = f'[\n{body},\n{indent}]'
    updated = src[:start] + replacement + src[end:]

    try:
        ast.parse(updated)
    except SyntaxError as e:
        return False, f"отказ: замена дала SyntaxError ({e}) — файл не тронут"

    backup = path.with_suffix(f'.py.bak.weekly.{datetime.now().strftime("%Y%m%d_%H%M%S")}')
    shutil.copy2(path, backup)
    path.write_text(updated)

    # Контроль: читаем обратно и сверяем состав
    try:
        check = ast.parse(path.read_text())
        got = None
        for stmt in check.body:
            if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == 'STRATEGIES' for t in stmt.targets):
                d = stmt.value.elts[0]
                for k, v in zip(d.keys, d.values):
                    if isinstance(k, ast.Constant) and k.value == 'symbols':
                        got = [e.value for e in v.elts]
        if got != list(new_symbols):
            raise ValueError(f"после записи прочитано {got}")
    except Exception as e:
        shutil.copy2(backup, path)
        return False, f"откат из бэкапа: проверка после записи не прошла ({e})"

    return True, f"symbols обновлён: {len(new_symbols)} пар (бэкап: {backup.name})"


# Score bonus filters (из v37)
def detect_fvg_soft(df, idx, direction='bullish'):
    if idx < 2: return False
    if direction == 'bullish':
        return df.iloc[idx]['low'] > df.iloc[idx-2]['high'] or df.iloc[idx]['low'] > df.iloc[idx-2]['open']
    elif direction == 'bearish':
        return df.iloc[idx]['high'] < df.iloc[idx-2]['low'] or df.iloc[idx]['high'] < df.iloc[idx-2]['open']
    return False

def detect_order_block_soft(df, idx, direction='bullish', lookback=10):
    if idx < lookback + 1: return False
    if direction == 'bullish':
        if df.iloc[idx]['close'] <= df.iloc[idx]['open']: return False
        for i in range(idx-1, max(0, idx-lookback-1), -1):
            if df.iloc[i]['close'] < df.iloc[i]['open']: return True
    elif direction == 'bearish':
        if df.iloc[idx]['close'] >= df.iloc[idx]['open']: return False
        for i in range(idx-1, max(0, idx-lookback-1), -1):
            if df.iloc[i]['close'] > df.iloc[i]['open']: return True
    return False

def detect_vsa_absorption_soft(df, idx, vol_threshold=1.5, spread_threshold=0.01):
    if idx < 20: return False
    avg_vol = df.iloc[idx-20:idx]['volume'].mean()
    current_vol = df.iloc[idx]['volume']
    if current_vol < avg_vol * vol_threshold: return False
    spread = (df.iloc[idx]['high'] - df.iloc[idx]['low']) / df.iloc[idx]['close']
    return spread < spread_threshold

def detect_htf_trend_soft(df_5m, idx, htf_period=240, direction='up'):
    if idx < htf_period: return False
    closes = df_5m.iloc[idx-htf_period:idx]['close'].values
    if len(closes) < 20: return False
    hourly_closes = closes[::12]
    if len(hourly_closes) < 20: return False
    sma10 = np.mean(hourly_closes[-10:])
    sma20 = np.mean(hourly_closes[-20:])
    if direction == 'up': return sma10 > sma20
    elif direction == 'down': return sma10 < sma20
    return False

def detect_kill_zone_soft(df_5m, idx):
    ts = pd.Timestamp(df_5m.index[idx])
    hour = ts.hour
    return (7 <= hour < 11) or (13 <= hour < 16)

def calculate_score(df, idx, direction):
    score = BASE_SCORE
    if detect_fvg_soft(df, idx, direction): score += SCORE_BONUSES['fvg']
    if detect_order_block_soft(df, idx, direction): score += SCORE_BONUSES['order_block']
    if detect_vsa_absorption_soft(df, idx): score += SCORE_BONUSES['vsa_absorption']
    htf_dir = 'up' if direction == 'bullish' else 'down'
    if detect_htf_trend_soft(df, idx, direction=htf_dir): score += SCORE_BONUSES['htf_trend']
    if detect_kill_zone_soft(df, idx): score += SCORE_BONUSES['kill_zone']
    return score

# CHOCH
def detect_swing_lows(df, idx, lookback=30):
    swings = []
    for i in range(max(0, idx - lookback), idx):
        if i < 2 or i >= len(df) - 2: continue
        if df.iloc[i]['low'] < df.iloc[i-1]['low'] and df.iloc[i]['low'] < df.iloc[i+1]['low']:
            swings.append((i, float(df.iloc[i]['low'])))
    return swings

def detect_swing_highs(df, idx, lookback=30):
    swings = []
    for i in range(max(0, idx - lookback), idx):
        if i < 2 or i >= len(df) - 2: continue
        if df.iloc[i]['high'] > df.iloc[i-1]['high'] and df.iloc[i]['high'] > df.iloc[i+1]['high']:
            swings.append((i, float(df.iloc[i]['high'])))
    return swings

def detect_choch_bullish(df, sweep_idx):
    swing_lows = detect_swing_lows(df, sweep_idx, lookback=30)
    if len(swing_lows) < 2: return None
    last_swing_low = swing_lows[-1][1]
    for i in range(sweep_idx + 2, min(sweep_idx + 10, len(df))):
        if float(df.iloc[i]['close']) > last_swing_low: return i
    return None

def detect_choch_bearish(df, sweep_idx):
    swing_highs = detect_swing_highs(df, sweep_idx, lookback=30)
    if len(swing_highs) < 2: return None
    last_swing_high = swing_highs[-1][1]
    for i in range(sweep_idx + 2, min(sweep_idx + 10, len(df))):
        if float(df.iloc[i]['close']) < last_swing_high: return i
    return None

def detect_v8_with_score(df, idx, params):
    if idx < params['swing_lookback']: return None, 0
    bar = df.iloc[idx]
    o, h, l, c = float(bar['open']), float(bar['high']), float(bar['low']), float(bar['close'])
    hour_utc = df.index[idx].hour
    if hour_utc < params['session_start_utc'] or hour_utc >= params['session_end_utc']: return None, 0
    lookback = df.iloc[max(0, idx-params['swing_lookback']):idx]
    sl_v = float(lookback['low'].min())
    sh_v = float(lookback['high'].max())
    body = abs(c - o)
    if body == 0: return None, 0
    lw = min(o, c) - l
    uw = h - max(o, c)
    bwk = lw / body
    swk = uw / body
    side = None
    if l < sl_v * (1 - params['sweep_threshold']) and c > sl_v and bwk >= params['wick_body_min_ratio']:
        choch_idx = detect_choch_bullish(df, idx)
        if choch_idx is not None: side = 'LONG'
    elif h > sh_v * (1 + params['sweep_threshold']) and c < sh_v and swk >= params['wick_body_min_ratio']:
        choch_idx = detect_choch_bearish(df, idx)
        if choch_idx is not None: side = 'SHORT'
    if side is None: return None, 0
    prev = df.iloc[max(0, idx-20):idx]
    va = float(prev['volume'].mean())
    if va <= 0: return None, 0
    vs = float(bar['volume']) / va
    if vs < params['min_volume_spike']: return None, 0
    direction = 'bullish' if side == 'LONG' else 'bearish'
    score = calculate_score(df, idx, direction)
    return side, score

def simulate_v8(side, entry, df, idx, params):
    is_long = side == "LONG"
    bar = df.iloc[idx]
    wick_low = float(bar['low'])
    wick_high = float(bar['high'])
    if is_long:
        sl = wick_low * 0.995
        risk = entry - sl
        tp = entry + risk * params['rr_ratio']
    else:
        sl = wick_high * 1.005
        risk = sl - entry
        tp = entry - risk * params['rr_ratio']
    trailing_sl = sl
    peak_price = entry
    bars_held = 0
    for j in range(1, params['max_hold_bars'] + 1):
        if idx + j >= len(df):
            cl = float(df.iloc[-1]['close'])
            pnl = ((cl - entry) / entry * 100 if is_long else (entry - cl) / entry * 100)
            return pnl - FEE_PCT
        bar = df.iloc[idx + j]
        hi, lo, cl = float(bar['high']), float(bar['low']), float(bar['close'])
        bars_held += 1
        if is_long: peak_price = max(peak_price, hi)
        else: peak_price = min(peak_price, lo)
        if bars_held >= 3:
            if is_long:
                profit_pct = (peak_price - entry) / entry * 100
                if profit_pct >= params['trailing_step_pct']:
                    new_sl = peak_price * (1 - params['trailing_step_pct'] / 100)
                    if new_sl > trailing_sl: trailing_sl = new_sl
            else:
                profit_pct = (entry - peak_price) / entry * 100
                if profit_pct >= params['trailing_step_pct']:
                    new_sl = peak_price * (1 + params['trailing_step_pct'] / 100)
                    if new_sl < trailing_sl: trailing_sl = new_sl
        if is_long:
            if lo <= trailing_sl:
                pnl = (trailing_sl - entry) / entry * 100
                return pnl - FEE_PCT
            if hi >= tp: return (tp - entry) / entry * 100 - FEE_PCT
        else:
            if hi >= trailing_sl:
                pnl = (entry - trailing_sl) / entry * 100
                return pnl - FEE_PCT
            if lo <= tp: return (entry - tp) / entry * 100 - FEE_PCT
    cl = float(df.iloc[min(idx + params['max_hold_bars'], len(df) - 1)]['close'])
    pnl = ((cl - entry) / entry * 100 if is_long else (entry - cl) / entry * 100)
    return pnl - FEE_PCT

def backtest_v8_score(df, params, start_idx, end_idx, min_score=MIN_SCORE):
    trades = []
    i = start_idx
    last = -999
    while i < end_idx:
        if i - last < 2:
            i += 1
            continue
        side, score = detect_v8_with_score(df, i, params)
        if not side or score < min_score:
            i += 1
            continue
        entry = float(df.iloc[i]['close'])
        pnl = simulate_v8(side, entry, df, i, params)
        trades.append({'pnl': pnl, 'score': score})
        last = i + params['max_hold_bars']
        i += 1
    return trades

def calc_metrics(trades):
    if len(trades) < 3: return None
    pnls = [t['pnl'] for t in trades]
    scores = [t['score'] for t in trades]
    n = len(pnls)
    wins = sum(1 for x in pnls if x > 0)
    wr = wins / n * 100
    gp = sum(x for x in pnls if x > 0)
    gl = abs(sum(x for x in pnls if x < 0))
    pf = gp / gl if gl > 0 else 9.99
    avg_win = gp / wins if wins > 0 else 0
    avg_loss = gl / (n - wins) if (n - wins) > 0 else 0
    exp = (wr / 100) * avg_win - (1 - wr / 100) * avg_loss
    return {'n': n, 'wr': wr, 'pf': pf, 'exp': exp, 'pnl': sum(pnls), 'avg_score': np.mean(scores)}


def main():
    """Главная функция еженедельной оптимизации."""
    print("=" * 100)
    print("  ЕЖЕНЕДЕЛЬНАЯ ОПТИМИЗАЦИЯ UNIVERSE")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 100)
    
    # 1. Получаем все Bybit linear pairs
    print("\n1. Получаю список Bybit linear perpetuals...")
    ex = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'linear'}})
    markets = ex.load_markets()
    
    linear_usdt = []
    for symbol, market in markets.items():
        if market.get('linear') and market.get('active') and symbol.endswith('/USDT:USDT'):
            linear_usdt.append(symbol)
    
    print(f"   Найдено {len(linear_usdt)} активных linear USDT perpetuals")
    
    # 2. Получаем tickers для объёма
    print("\n2. Получаю tickers (24h volume)...")
    tickers = ex.fetch_tickers()
    
    pairs_with_volume = []
    for symbol in linear_usdt:
        if symbol not in tickers:
            continue
        ticker = tickers[symbol]
        quote_volume = ticker.get('quoteVolume', 0) or 0
        if quote_volume > 1_000_000:  # > $1M/день
            pairs_with_volume.append({
                'symbol': symbol,
                'volume_24h_usd': quote_volume,
            })
    
    print(f"   Пар с объёмом > $1M/день: {len(pairs_with_volume)}")
    
    # 3. Топ-50 по объёму
    candidates_sorted = sorted(pairs_with_volume, key=lambda x: -x['volume_24h_usd'])[:50]
    
    print(f"\n3. Топ-50 кандидатов по 24h volume")
    
    # 4. Загружаем OHLCV 90д
    print(f"\n4. Загружаю OHLCV 90д для {len(candidates_sorted)} пар...")
    loaded = []
    for i, c in enumerate(candidates_sorted, 1):
        symbol = c['symbol']
        # [Fix 03.08.2026 Claude] Было: symbol.replace('/','').replace(':','') — для
        # unified-символа ccxt «US/USDT:USDT» давало «USUSDTUSDT» (двойной суффикс).
        # Такие имена попадали в universe.json, v8_per_pair_params.json и в боевой
        # STRATEGIES["symbols"] → пары неисполнимы. Settle-часть после ':' отбрасываем.
        sym_clean = symbol.split(':')[0].replace('/', '')
        print(f"   [{i}/{len(candidates_sorted)}] {symbol}...", end=' ', flush=True)
        
        try:
            all_ohlcv = []
            since = int((time.time() - 90 * 24 * 3600) * 1000)
            
            for _ in range(26):
                ohlcv = ex.fetch_ohlcv(symbol, '5m', since=since, limit=1000)
                if not ohlcv:
                    break
                all_ohlcv.extend(ohlcv)
                since = ohlcv[-1][0] + 1
                time.sleep(0.15)
            
            if len(all_ohlcv) < 5000:
                print(f"❌ мало данных ({len(all_ohlcv)} баров)")
                continue
            
            df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df = df[~df.index.duplicated(keep='last')]
            df.sort_index(inplace=True)
            
            output_path = CLAUDE_DATA / f"ohlcv_{sym_clean}_90d.pkl"
            with open(output_path, 'wb') as f:
                pickle.dump(df, f)
            
            print(f"✅ {len(df)} баров")
            loaded.append({'symbol': symbol, 'sym_clean': sym_clean, 'bars': len(df)})
            
        except Exception as e:
            print(f"❌ {e}")
    
    print(f"\n✅ Загружено: {len(loaded)} пар")
    
    # 5. Тестируем V8 + Score
    print(f"\n5. Тестирую V8 + Score на {len(loaded)} парах...")
    
    bars_per_day = 288
    train_bars = 30 * bars_per_day
    test_bars = 7 * bars_per_day
    
    results = []
    
    for item in loaded:
        sym = item['symbol']
        sym_clean = item['sym_clean']
        path = CLAUDE_DATA / f"ohlcv_{sym_clean}_90d.pkl"
        
        with open(path, 'rb') as f:
            df = pickle.load(f)
        
        train_end = len(df)
        train_start = max(100, train_end - train_bars)
        test_end = train_start
        test_start = max(100, test_end - test_bars)
        
        if test_start >= test_end or train_start >= train_end:
            continue
        
        train_trades = backtest_v8_score(df, V8_BASE, train_start, train_end)
        test_trades = backtest_v8_score(df, V8_BASE, test_start, test_end)
        
        train_metrics = calc_metrics(train_trades)
        test_metrics = calc_metrics(test_trades)
        
        if train_metrics and test_metrics:
            results.append({
                'symbol': sym,
                'sym_clean': sym_clean,
                'train_n': train_metrics['n'], 'train_wr': train_metrics['wr'],
                'train_pf': train_metrics['pf'], 'train_exp': train_metrics['exp'],
                'test_n': test_metrics['n'], 'test_wr': test_metrics['wr'],
                'test_pf': test_metrics['pf'], 'test_exp': test_metrics['exp'],
            })
    
    # 6. Выбираем прибыльные
    profitable = [r for r in results if r['test_n'] >= 3 and r['test_exp'] > 0]
    profitable_sorted = sorted(profitable, key=lambda x: -x['test_exp'])
    
    print(f"\n6. Прибыльных пар: {len(profitable)}/{len(results)}")
    
    if profitable_sorted:
        print(f"\n   ТОП прибыльных:")
        for r in profitable_sorted[:15]:
            print(f"   {r['symbol']:<20}  test: n={r['test_n']} WR={r['test_wr']:.1f}% PF={r['test_pf']:.2f} exp={r['test_exp']:+.4f}%")
    
    # 7. Per-pair оптимизация параметров (grid search)
    print(f"\n7. Per-pair оптимизация параметров для {len(profitable)} прибыльных пар...")
    
    per_pair_params = {}
    
    for r in profitable_sorted:
        sym = r['symbol']
        sym_clean = r['sym_clean']
        path = CLAUDE_DATA / f"ohlcv_{sym_clean}_90d.pkl"
        
        if not path.exists():
            continue
        
        with open(path, 'rb') as f:
            df = pickle.load(f)
        
        train_end = len(df)
        train_start = max(100, train_end - train_bars)
        
        if train_start >= train_end:
            continue
        
        # Grid search для этой пары
        best_params = None
        best_exp = -999
        
        # Упрощённый grid (быстрее)
        SWEEP_GRID = [0.002, 0.003, 0.005]
        WICK_GRID = [0.8, 1.0, 1.2]
        VOL_GRID = [1.0, 1.3, 1.5]
        TRAILING_GRID = [0.20, 0.30, 0.40]
        RR_GRID = [2.0, 2.5, 3.0]
        
        from itertools import product
        
        for sweep, wick, vol, trail, rr in product(SWEEP_GRID, WICK_GRID, VOL_GRID, TRAILING_GRID, RR_GRID):
            params = {
                'swing_lookback': 50,
                'sweep_threshold': sweep,
                'wick_body_min_ratio': wick,
                'min_volume_spike': vol,
                'session_start_utc': 6,
                'session_end_utc': 20,
                'trailing_step_pct': trail,
                'max_hold_bars': 36,
                'rr_ratio': rr,
                'choch_lookback': 10,
                'choch_min_bars': 2,
            }
            
            train_trades = backtest_v8_score(df, params, train_start, train_end)
            train_metrics = calc_metrics(train_trades)
            
            if train_metrics and train_metrics['exp'] > best_exp:
                best_exp = train_metrics['exp']
                best_params = params.copy()
        
        if best_params:
            per_pair_params[sym_clean] = {
                'sweep_threshold': best_params['sweep_threshold'],
                'wick_body_min_ratio': best_params['wick_body_min_ratio'],
                'min_volume_spike': best_params['min_volume_spike'],
                'trailing_step_pct': best_params['trailing_step_pct'],
                'rr_ratio': best_params['rr_ratio'],
                'train_exp': best_exp,
            }
            print(f"   {sym_clean}: sweep={best_params['sweep_threshold']:.3f} wick={best_params['wick_body_min_ratio']:.1f} vol={best_params['min_volume_spike']:.1f} trail={best_params['trailing_step_pct']:.2f} rr={best_params['rr_ratio']:.1f} exp={best_exp:+.4f}%")
    
    # 8. Сохраняем per-pair params
    V8_PER_PAIR_JSON = Path("/home/andy/CryptoTrader_main/config/v8_per_pair_params.json")
    V8_PER_PAIR_JSON.parent.mkdir(parents=True, exist_ok=True)
    
    with open(V8_PER_PAIR_JSON, 'w') as f:
        json.dump({
            'updated_at': datetime.now().isoformat(),
            'params': per_pair_params,
        }, f, indent=2)
    
    print(f"\n8. ✅ Сохранено: {V8_PER_PAIR_JSON} ({len(per_pair_params)} пар)")
    
    # 9. Обновляем universe.json
    new_universe = [r['sym_clean'] for r in profitable_sorted]
    
    UNIVERSE_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(UNIVERSE_JSON, 'w') as f:
        json.dump({
            'updated_at': datetime.now().isoformat(),
            'symbols': new_universe,
            'count': len(new_universe),
        }, f, indent=2)
    
    print(f"\n9. ✅ Обновлён universe.json: {len(new_universe)} пар")
    
    # 10. Обновляем clone5_multi_runner.py
    print(f"\n10. Обновляю clone5_multi_runner.py...")

    ok, msg = update_runner_symbols(new_universe)
    print(f"   {'✅' if ok else '❌'} {msg}")
    
    # 9. Сохраняем отчёт
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_file = REPORTS_DIR / f"weekly_optimization_{datetime.now().strftime('%Y%m%d')}.json"
    
    with open(report_file, 'w') as f:
        json.dump({
            'timestamp': datetime.now().isoformat(),
            'total_candidates': len(candidates_sorted),
            'loaded': len(loaded),
            'tested': len(results),
            'profitable': len(profitable),
            'new_universe': new_universe,
            'top_pairs': profitable_sorted[:15],
            'all_results': results,
        }, f, indent=2)
    
    print(f"\n9. ✅ Отчёт сохранён: {report_file}")
    
    print(f"\n{'='*100}")
    print(f"  ГОТОВО")
    print(f"  Новых пар в universe: {len(new_universe)}")
    print(f"  Топ-3: {', '.join(new_universe[:3])}")
    print(f"{'='*100}")


if __name__ == "__main__":
    main()
