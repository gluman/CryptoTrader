#!/usr/bin/env python3
"""Еженедельный подбор персональных SL/TP/max_hold для стратегии v9 CTL.

ЧЕМ ОТЛИЧАЕТСЯ ОТ СТАРОГО weekly_optimization.py (04.08.2026):
  • НЕ подбирает условия входа. Вход у v9 зафиксирован и проверен вне выборки;
    свободный подбор входа+фильтров+выхода давал 164 095 «прибыльных» комбинаций
    на train и −0.15…−0.19%/сделку на holdout — хуже случайного выбора.
    Подбирается только выход (SL/TP/max_hold) — пространство маленькое, и в таком
    виде подбор переносится вперёд: +0.144%/сделку на holdout, 22/26 пар в плюсе.
  • НЕ пишет в боевые .py. Результат идёт только в config/v9_per_pair_params.json.
  • Берётся не пиковая комбинация, а центр плато (медиана топ-5) — устойчивее к шуму.
  • Кандидат обязан быть прибыльным и на train, и на valid; иначе пара получает
    глобальные дефолты вместо персональных значений.

Запуск: python cryptotrader_strategies/weekly_v9_optim.py [--dry-run]
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import ccxt

sys.path.insert(0, "/home/andy/CryptoTrader_main")
from cryptotrader_strategies.clone_5_v9 import (  # noqa: E402
    CTL_LOOKBACK, CTL_SWEEP_THRESHOLD, CTL_WICK_BODY_MIN, CTL_MIN_VOLUME_SPIKE,
    CTL_SESSION_START_UTC, CTL_SESSION_END_UTC, CTL_FOMO_MAX_BAR_RETURN_PCT,
    CTL_DEFAULT_SL, CTL_DEFAULT_TP, CTL_DEFAULT_MAX_HOLD_MIN, V9_PARAMS_PATH,
)
from cryptotrader_strategies.clone5_multi_runner import STRATEGIES  # noqa: E402

FEE_RT = 0.11
DAYS = 90
SLS = [0.6, 1.0, 1.5, 2.0, 2.5, 3.5]
TPS = [1.0, 1.5, 2.0, 2.5, 3.5, 5.0]
MHS_BARS = [12, 36, 72, 144]          # 60м, 180м, 360м, 720м
MIN_TRADES_TRAIN = 20
MIN_TRADES_VALID = 8


def fetch(ex, unified, tf, step_ms, since_ms, end_ms):
    bars, since = [], since_ms
    while since < end_ms:
        chunk = ex.fetch_ohlcv(unified, tf, since=since, limit=1000)
        if not chunk:
            break
        bars.extend(chunk)
        nxt = chunk[-1][0] + step_ms
        if nxt <= since:
            break
        since = nxt
        if len(chunk) < 1000:
            break
        time.sleep(0.05)
    seen, out = set(), []
    for b in bars:
        if b[0] not in seen:
            seen.add(b[0]); out.append(b)
    out.sort(key=lambda x: x[0])
    return np.array(out, dtype=float) if out else None


def signals_ctl(m5, h1):
    """Индексы баров-сигналов v9 CTL. Логика идентична Clone5V9Strategy.decide()."""
    ts, o, h, l, c, v = (m5[:, i] for i in range(6))
    n = len(m5)
    hour = pd.to_datetime(ts, unit="ms", utc=True).hour.values
    body = np.abs(c - o)
    body = np.where(body < 1e-10, np.abs(h - l) * 0.1, body)
    wick = (np.minimum(o, c) - l) / np.maximum(body, 1e-10)
    bar_ret = np.abs((c - o) / np.maximum(o, 1e-12)) * 100
    vol_avg = pd.Series(v).rolling(20).mean().shift(1).values
    vol_spike = v / np.where(vol_avg > 0, vol_avg, np.nan)
    swing_low = pd.Series(l).rolling(CTL_LOOKBACK).min().shift(1).values
    sweep = (swing_low - l) / np.where(swing_low > 0, swing_low, np.nan)

    # часовой тренд: SMA20/SMA50 + зона flat ±0.3% (как в боевом compute_higher_tf_trends)
    trend = np.zeros(n)
    if h1 is not None and len(h1) > 55:
        ch = h1[:, 4]
        s20 = pd.Series(ch).rolling(20).mean().shift(1).values
        s50 = pd.Series(ch).rolling(50).mean().shift(1).values
        diff = (s20 - s50) / np.where(s50 > 0, s50, np.nan) * 100
        tr = np.where(np.isnan(diff), 0.0, np.where(diff > 0.3, 1.0, np.where(diff < -0.3, -1.0, 0.0)))
        idx = np.searchsorted(h1[:, 0], ts, side="right") - 1
        ok = idx >= 0
        trend[ok] = tr[idx[ok]]

    m = ((sweep >= CTL_SWEEP_THRESHOLD) & (c > swing_low) & (wick >= CTL_WICK_BODY_MIN)
         & (vol_spike >= CTL_MIN_VOLUME_SPIKE) & (hour >= CTL_SESSION_START_UTC)
         & (hour < CTL_SESSION_END_UTC) & (bar_ret <= CTL_FOMO_MAX_BAR_RETURN_PCT)
         & (trend < 0))
    return np.where(np.nan_to_num(m, nan=0).astype(bool))[0]


def simulate(m5, idx, sl, tp, mh_bars):
    """Выходы: вход по open следующего бара, SL приоритетнее TP в одном баре."""
    o, h, l, c = m5[:, 1], m5[:, 2], m5[:, 3], m5[:, 4]
    n = len(m5)
    out, tss = [], []
    for i in idx:
        i0 = int(i) + 1
        if i0 >= n - 1:
            continue
        entry = o[i0]
        if entry <= 0:
            continue
        stop, take = entry * (1 - sl / 100), entry * (1 + tp / 100)
        end = min(i0 + mh_bars, n)
        pnl = None
        for j in range(i0, end):
            if l[j] <= stop:
                pnl = (stop / entry - 1) * 100; break
            if h[j] >= take:
                pnl = (take / entry - 1) * 100; break
        if pnl is None:
            pnl = (c[end - 1] / entry - 1) * 100
        out.append(pnl - FEE_RT)
        tss.append(m5[i0, 0])
    return np.array(out), np.array(tss)


def main():
    dry = "--dry-run" in sys.argv
    symbols = STRATEGIES[0]["symbols"]
    ex = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = int((datetime.now(timezone.utc) - timedelta(days=DAYS)).timestamp() * 1000)

    print("=" * 84)
    print(f"  ЕЖЕНЕДЕЛЬНЫЙ ПОДБОР SL/TP/max_hold ДЛЯ v9 CTL — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  вход зафиксирован и не подбирается; {len(symbols)} пар × {DAYS} дней")
    print("=" * 84)

    params, stats_rows = {}, []
    for i, sym in enumerate(symbols, 1):
        u = f"{sym[:-4]}/USDT:USDT"
        try:
            m5 = fetch(ex, u, "5m", 300_000, start, end)
            h1 = fetch(ex, u, "1h", 3_600_000, start, end)
        except Exception as e:
            print(f"[{i}/{len(symbols)}] {sym}: ошибка загрузки — {e}")
            continue
        if m5 is None or len(m5) < 3000:
            print(f"[{i}/{len(symbols)}] {sym}: мало данных → дефолты")
            params[sym] = {"sl_pct": CTL_DEFAULT_SL, "tp_pct": CTL_DEFAULT_TP,
                           "max_hold_min": CTL_DEFAULT_MAX_HOLD_MIN, "source": "default_no_data"}
            continue

        idx = signals_ctl(m5, h1)
        if len(idx) < MIN_TRADES_TRAIN + MIN_TRADES_VALID:
            print(f"[{i}/{len(symbols)}] {sym}: сигналов {len(idx)} — мало → дефолты")
            params[sym] = {"sl_pct": CTL_DEFAULT_SL, "tp_pct": CTL_DEFAULT_TP,
                           "max_hold_min": CTL_DEFAULT_MAX_HOLD_MIN, "source": "default_few_signals"}
            continue

        split = m5[0, 0] + (m5[-1, 0] - m5[0, 0]) * 0.70
        scored = []
        for sl in SLS:
            for tp in TPS:
                for mh in MHS_BARS:
                    pnl, tss = simulate(m5, idx, sl, tp, mh)
                    if len(pnl) == 0:
                        continue
                    tr, va = pnl[tss < split], pnl[tss >= split]
                    if len(tr) < MIN_TRADES_TRAIN or len(va) < MIN_TRADES_VALID:
                        continue
                    if tr.mean() <= 0 or va.mean() <= 0:      # обязателен плюс на обоих
                        continue
                    scored.append((tr.mean() + va.mean(), sl, tp, mh, pnl))

        if not scored:
            print(f"[{i}/{len(symbols)}] {sym}: подтверждения на обоих периодах нет → дефолты")
            params[sym] = {"sl_pct": CTL_DEFAULT_SL, "tp_pct": CTL_DEFAULT_TP,
                           "max_hold_min": CTL_DEFAULT_MAX_HOLD_MIN, "source": "default_not_confirmed"}
            continue

        scored.sort(key=lambda x: -x[0])
        top = scored[:5]
        sl = min(SLS, key=lambda g: abs(g - float(np.median([t[1] for t in top]))))
        tp = min(TPS, key=lambda g: abs(g - float(np.median([t[2] for t in top]))))
        mh = min(MHS_BARS, key=lambda g: abs(g - float(np.median([t[3] for t in top]))))
        pnl, _ = simulate(m5, idx, sl, tp, mh)
        exp = float(pnl.mean())
        wr = float(100 * (pnl > 0).mean())
        params[sym] = {"sl_pct": sl, "tp_pct": tp, "max_hold_min": mh * 5,
                       "backtest_n": int(len(pnl)), "backtest_exp": round(exp, 3),
                       "backtest_wr": round(wr, 1), "source": "per_pair_confirmed"}
        stats_rows.append((sym, exp))
        print(f"[{i}/{len(symbols)}] {sym}: SL {sl}% / TP {tp}% / MH {mh*5}м → "
              f"exp {exp:+.3f}%/сделку, WR {wr:.1f}%, n={len(pnl)}")

    conf = sum(1 for v in params.values() if v.get("source") == "per_pair_confirmed")
    print("-" * 84)
    print(f"персональные параметры: {conf} пар, дефолты: {len(params) - conf}")
    if stats_rows:
        e = np.array([x[1] for x in stats_rows])
        print(f"средний exp: {e.mean():+.3f}%/сделку, в плюсе {(e > 0).sum()}/{len(e)}")

    if dry:
        print("\n--dry-run: файл не записан")
        return 0

    out = {
        "updated_at": datetime.now().isoformat(),
        "source": "weekly_v9_optim.py — подбор ТОЛЬКО выхода при зафиксированном входе v9 CTL",
        "method": f"{DAYS}д 5m, центр плато (медиана топ-5), обязателен плюс на train и valid, комиссия {FEE_RT}% RT",
        "entry": {"lookback": CTL_LOOKBACK, "sweep_threshold": CTL_SWEEP_THRESHOLD,
                  "wick_body_min_ratio": CTL_WICK_BODY_MIN, "min_volume_spike": CTL_MIN_VOLUME_SPIKE,
                  "session_utc": [CTL_SESSION_START_UTC, CTL_SESSION_END_UTC],
                  "side": "LONG only", "trend_filter": "1h trend must be down"},
        "params": params,
    }
    prev = Path(str(V9_PARAMS_PATH) + f".bak.{datetime.now():%Y%m%d_%H%M%S}")
    if V9_PARAMS_PATH.exists():
        prev.write_text(V9_PARAMS_PATH.read_text())
    V9_PARAMS_PATH.write_text(json.dumps(out, indent=2))
    print(f"\n✅ записано: {V9_PARAMS_PATH} (бэкап: {prev.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
