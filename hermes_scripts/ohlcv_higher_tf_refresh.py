#!/usr/bin/env python3
"""
ohlcv_higher_tf_refresh.py — обновление 1h/4h OHLCV для v7a пар.

ЗАЧЕМ:
  После reboot сервера (12.07.2026) БД Postgres очищается, и старые 1h/4h
  данные теряются. При перезагрузке сканер не может посчитать trend (5/8 пар
  = 'unknown') → 0 сигналов.

  Этот скрипт обновляет 1h/4h OHLCV за последние 60 дней с Bybit public API.
  On-demand (каждый час cron).

ЧТО ДЕЛАЕТ:
  Для каждой v7a пары (8 пар) и TF (1h, 4h):
  - Fetch OHLCV за 60 дней (1 запрос — limit=1000 баров покрывает 60+ дней)
  - UPSERT в ohlcv_raw через ON CONFLICT

ПОЧЕМУ ОТДЕЛЬНЫЙ КРОН:
  - Phase 0 в scan_and_execute.py собирает ТОЛЬКО 5m каждый тик (every 5m)
  - 1h/4h обновляются редко (раз в час достаточно, т.к. бар крупный)
  - Избегаем дублирования запросов к Bybit при каждом тике scanner'а

ЗАПУСК:
  Cron every 1h:
    /home/andy/cryptotrader-venv/bin/python /home/andy/CryptoTrader_main/scripts/ohlcv_higher_tf_refresh.py

ПАРАМЕТРЫ:
  Pairs: 8 v7a пар (SUI/NEAR/SOL/LIT/WLD/DOGE/ADA/XRP × USDT)
  TF:    1h, 4h
  Days:  60
  Quota: 8 пар × 2 TF × 1 запрос = 16 req/hour (низкий)

СМ. ТАКЖЕ:
  - scan_and_execute.py phase0_collect (5m collector, every 5m)
  - cryptotrader_strategies/clone5_multi_runner.py (consumer)
"""
from __future__ import annotations
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import ccxt

from dotenv import load_dotenv
load_dotenv("/home/andy/CryptoTrader_main/.env", override=True)

from sqlalchemy import create_engine, text as sa_text

# ── Config ────────────────────────────────────────────────────────────────────
# [Fix 05.08.2026 Claude] Раньше здесь был захардкоженный список из 19 пар. Он отстал
# от боевого: у 7 добавленных 30.07 пар (SPCX, BEAT, KAITO, UAI, EUL, RE, GIGGLE) не
# собирались часовые бары → compute_higher_tf_trends отдавал по ним "unknown" →
# стратегия v9 их вообще не торговала (ей нужен явный тренд "down"). Теперь список
# берётся из STRATEGIES — единственного источника правды — и не может разъехаться.
def _load_pairs() -> list[str]:
    try:
        sys.path.insert(0, "/home/andy/CryptoTrader_main")
        from cryptotrader_strategies.clone5_multi_runner import STRATEGIES
        pairs = list(dict.fromkeys(s for cfg in STRATEGIES for s in cfg["symbols"]))
        if pairs:
            return pairs
    except Exception as e:
        print(f"⚠ не удалось прочитать STRATEGIES ({e}) — беру аварийный список")
    return ["SUIUSDT", "NEARUSDT", "SOLUSDT", "LITUSDT",
            "DOGEUSDT", "ADAUSDT",
            "AKEUSDT", "SOXLUSDT", "ZECUSDT", "ENAUSDT",
            "APTUSDT", "TAOUSDT", "AVAXUSDT", "AAVEUSDT",
            "BCHUSDT", "DYDXUSDT", "AXSUSDT", "MAGICUSDT", "KSMUSDT"]


PAIRS = _load_pairs()

# (ccxt_tf, days_history, label)
TIMEFRAMES = [
    ("15m", 60),
    ("1h", 60),
    ("4h", 60),
]

PG_URL = (
    f"postgresql+psycopg2://{os.getenv('POSTGRES_USER', 'cryptotrader')}:"
    f"{os.getenv('POSTGRES_PASSWORD', 'cryptotrader123')}@"
    f"{os.getenv('POSTGRES_HOST', '127.0.0.1')}:"
    f"{os.getenv('POSTGRES_PORT', '5432')}/"
    f"{os.getenv('POSTGRES_DB', 'cryptotrader')}"
)
engine = create_engine(PG_URL, pool_size=4)

ex = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})
ex.load_markets()


def upsert_bars(symbol: str, tf: str, ohlcv_list: list) -> tuple[int, int]:
    if not ohlcv_list:
        return 0, 0
    inserted = 0
    updated = 0
    with engine.begin() as session:
        for candle in ohlcv_list:
            ts = datetime.fromtimestamp(candle[0] / 1000, tz=timezone.utc)
            o, h, l, c, v = candle[1], candle[2], candle[3], candle[4], candle[5]
            qv = float(v) * float(c)
            res = session.execute(
                sa_text("""
                    INSERT INTO ohlcv_raw
                        (exchange, symbol, timeframe, timestamp,
                         open, high, low, close, volume, quote_volume, trades_count)
                    VALUES ('bybit', :sym, :tf, :ts, :o, :h, :l, :c, :v, :qv, 0)
                    ON CONFLICT (exchange, symbol, timeframe, timestamp)
                    DO UPDATE SET
                        open = EXCLUDED.open, high = EXCLUDED.high,
                        low = EXCLUDED.low, close = EXCLUDED.close,
                        volume = EXCLUDED.volume, quote_volume = EXCLUDED.quote_volume
                    RETURNING (xmax = 0) AS was_inserted
                """),
                {"sym": symbol, "tf": tf, "ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v, "qv": qv},
            )
            row = res.fetchone()
            if row and row[0]:
                inserted += 1
            else:
                updated += 1
    return inserted, updated


def main():
    started = datetime.now(timezone.utc)
    now_msk = (started + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M:%S")
    print(f"═══ Higher-TF OHLCV refresh started: {now_msk} MSK ═══\n")

    total_ins = 0
    total_upd = 0

    for tf, days in TIMEFRAMES:
        print(f"── TF: {tf} ({days}d) ──")
        for sym in PAIRS:
            t0 = time.time()
            try:
                # Bybit limit per call = 1000 bars. 60d × 24 = 1440 для 1h,
                # но limit=1000 = ~42 дня. Нужно 2 запроса для 1h.
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=days)
                since_ms = int(start.timestamp() * 1000)
                end_ms = int(end.timestamp() * 1000)

                all_bars = []
                cursor = since_ms
                calls = 0
                while cursor < end_ms and calls < 3:
                    batch = ex.fetch_ohlcv(sym, tf, since=cursor, limit=1000)
                    if not batch:
                        break
                    all_bars.extend(batch)
                    last_ts = batch[-1][0]
                    if last_ts <= cursor:
                        break
                    cursor = last_ts + 1
                    calls += 1
                    if len(batch) < 1000:
                        break
                    time.sleep(0.15)

                ins, upd = upsert_bars(sym, tf, all_bars)
                elapsed = time.time() - t0
                total_ins += ins
                total_upd += upd
                print(f"  {sym:8s}  fetched={len(all_bars):>5d}  ins={ins:>4d}  upd={upd:>4d}  ({elapsed:.1f}s)")
            except Exception as e:
                print(f"  {sym:8s}  ERROR: {str(e)[:80]}")
            time.sleep(0.3)

    finished = datetime.now(timezone.utc)
    dur = (finished - started).total_seconds()
    print(f"\n═══ Done in {dur:.1f}s  inserted={total_ins}  updated={total_upd} ═══")


if __name__ == "__main__":
    main()