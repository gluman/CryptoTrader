"""
OHLCV Processing Pipeline — вычисляет индикаторы из ohlcv_raw и сохраняет в ohlcv_processed.

Запуск:
    cd /home/andy && ./cryptotrader-venv/bin/python src/agents/ohlcv_pipeline.py

Индикаторы:
    SMA: 20, 50, 200
    EMA: 12, 26
    RSI: 14
    MACD: (12, 26, 9)
    ATR: 14
    Bollinger Bands: (20, 2.0)
    CSS: Cumulative Swing Strength
    Volume: SMA 20 + ratio
"""

import sys
import logging
from datetime import datetime, timedelta, timezone as tz

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/andy")

from src.core.config import Config
from src.core.database import DatabaseManager
from sqlalchemy import text

logger = logging.getLogger("ohlcv_pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute technical indicators on OHLCV DataFrame (columns: open, high, low, close, volume)."""
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = df["volume"].astype(float)

    # SMA
    df["sma_20"] = c.rolling(20).mean()
    df["sma_50"] = c.rolling(50).mean()
    df["sma_200"] = c.rolling(200).mean()

    # EMA
    df["ema_12"] = c.ewm(span=12, adjust=False).mean()
    df["ema_26"] = c.ewm(span=26, adjust=False).mean()

    # RSI 14
    delta = c.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi_14"] = 100.0 - (100.0 / (1.0 + rs))

    # MACD (12, 26, 9)
    df["macd"] = df["ema_12"] - df["ema_26"]
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # ATR 14
    tr1 = h - l
    tr2 = (h - c.shift(1)).abs()
    tr3 = (l - c.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14).mean()

    # Bollinger Bands (20, 2.0)
    df["bollinger_middle"] = df["sma_20"]
    bb_std = c.rolling(20).std()
    df["bollinger_upper"] = df["bollinger_middle"] + 2.0 * bb_std
    df["bollinger_lower"] = df["bollinger_middle"] - 2.0 * bb_std

    # CSS — cumulative swing strength
    direction = c.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    swing = direction * (h - l) / c.shift(1) * 100
    df["css_value"] = swing.rolling(20).sum()
    df["css_prior"] = df["css_value"].shift(1)

    # Volume
    df["volume_sma_20"] = v.rolling(20).mean()
    df["volume_ratio"] = v / df["volume_sma_20"].replace(0, np.nan)

    return df


def run_once(db: DatabaseManager, limit: int = 500):
    """Process latest OHLCV data for all exchange/symbol/timeframe combos."""
    logger.info("=== OHLCV Processing Pipeline ===")

    # Get combos — use its own session
    with db.get_session() as session:
        combos = session.execute(text(
            "SELECT DISTINCT exchange, symbol, timeframe FROM ohlcv_raw ORDER BY exchange, symbol, timeframe"
        )).fetchall()
        logger.info(f"Found {len(combos)} combos")

    total = 0

    # Process each combo in its OWN session — errors isolated
    for exchange, symbol, timeframe in combos:
        try:
            with db.get_session() as session:
                raw = session.execute(text("""
                    SELECT timestamp, open, high, low, close, volume
                    FROM ohlcv_raw
                    WHERE exchange = :ex AND symbol = :sym AND timeframe = :tf
                    ORDER BY timestamp DESC
                    LIMIT :lim
                """), {"ex": exchange, "sym": symbol, "tf": timeframe, "lim": limit}).fetchall()

                if not raw:
                    continue

                df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
                # Convert Decimals to float
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = df[col].astype(float)

                df = df.iloc[::-1].reset_index(drop=True)
                df = compute_indicators(df)
                df = df.dropna(subset=["close"])

                if df.empty:
                    continue

                insert_sql = """
                    INSERT INTO ohlcv_processed (
                        exchange, symbol, timeframe, timestamp,
                        open, high, low, close, volume,
                        sma_20, sma_50, sma_200, ema_12, ema_26,
                        rsi_14, macd, macd_signal, macd_hist,
                        atr_14, bollinger_upper, bollinger_middle, bollinger_lower,
                        css_value, css_prior, volume_sma_20, volume_ratio,
                        created_at
                    ) VALUES (
                        :ex, :sym, :tf, :ts,
                        :open, :high, :low, :close, :vol,
                        :sma_20, :sma_50, :sma_200, :ema_12, :ema_26,
                        :rsi, :macd, :macd_sig, :macd_hist,
                        :atr, :bb_u, :bb_m, :bb_l,
                        :css, :css_p, :vol_sma, :vol_ratio, NOW()
                    )
                    ON CONFLICT (exchange, symbol, timeframe, timestamp)
                    DO UPDATE SET
                        open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                        close=EXCLUDED.close, volume=EXCLUDED.volume,
                        sma_20=EXCLUDED.sma_20, sma_50=EXCLUDED.sma_50, sma_200=EXCLUDED.sma_200,
                        ema_12=EXCLUDED.ema_12, ema_26=EXCLUDED.ema_26, rsi_14=EXCLUDED.rsi_14,
                        macd=EXCLUDED.macd, macd_signal=EXCLUDED.macd_signal, macd_hist=EXCLUDED.macd_hist,
                        atr_14=EXCLUDED.atr_14,
                        bollinger_upper=EXCLUDED.bollinger_upper, bollinger_middle=EXCLUDED.bollinger_middle,
                        bollinger_lower=EXCLUDED.bollinger_lower,
                        css_value=EXCLUDED.css_value, css_prior=EXCLUDED.css_prior,
                        volume_sma_20=EXCLUDED.volume_sma_20, volume_ratio=EXCLUDED.volume_ratio,
                        created_at=NOW()
                """

                def safe_val(v, decimals=8):
                    if pd.isna(v):
                        return None
                    f = float(v)
                    if abs(f) < 1e-7:
                        return None  # too small for Decimal(20,8)
                    if abs(f) > 1e15:
                        return None  # overflow guard
                    return round(f, decimals)

                count = 0
                for _, row in df.iterrows():
                    try:
                        ts = row["timestamp"]
                        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                            from datetime import timezone as tz, timedelta
                            ts = ts.replace(tzinfo=tz(timedelta(hours=3)))

                        params = {
                            "ex": exchange, "sym": symbol, "tf": timeframe, "ts": ts,
                            "open": safe_val(row["open"]),
                            "high": safe_val(row["high"]),
                            "low": safe_val(row["low"]),
                            "close": safe_val(row["close"]),
                            "vol": safe_val(row["volume"]),
                            "sma_20": safe_val(row.get("sma_20")),
                            "sma_50": safe_val(row.get("sma_50")),
                            "sma_200": safe_val(row.get("sma_200")),
                            "ema_12": safe_val(row.get("ema_12")),
                            "ema_26": safe_val(row.get("ema_26")),
                            "rsi": safe_val(row.get("rsi_14"), decimals=4),
                            "macd": safe_val(row.get("macd")),
                            "macd_sig": safe_val(row.get("macd_signal")),
                            "macd_hist": safe_val(row.get("macd_hist")),
                            "atr": safe_val(row.get("atr_14")),
                            "bb_u": safe_val(row.get("bollinger_upper")),
                            "bb_m": safe_val(row.get("bollinger_middle")),
                            "bb_l": safe_val(row.get("bollinger_lower")),
                            "css": safe_val(row.get("css_value"), decimals=4),
                            "css_p": safe_val(row.get("css_prior"), decimals=4),
                            "vol_sma": safe_val(row.get("volume_sma_20")),
                            "vol_ratio": safe_val(row.get("volume_ratio"), decimals=4),
                        }
                        session.execute(text(insert_sql), params)
                        count += 1
                    except Exception as e:
                        logger.debug(f"  skip row: {e}")
                        continue

                total += count
                logger.info(f"  {exchange}/{symbol}/{timeframe}: {count} rows")

        except Exception as e:
            logger.warning(f"  SKIP {exchange}/{symbol}/{timeframe}: {e}")
            continue

    # Final summary
    try:
        with db.get_session() as session:
            total_rows = session.execute(text("SELECT COUNT(*) FROM ohlcv_processed")).scalar()
            latest = session.execute(text("SELECT MAX(created_at) FROM ohlcv_processed")).scalar()
            logger.info(f"=== Done: {total_rows} total rows, latest: {latest} ===")
    except Exception as e:
        logger.error(f"Summary check failed: {e}")


if __name__ == "__main__":
    cfg = Config.load()
    db = DatabaseManager(cfg.postgresql, logging.getLogger("ohlcv_pipeline"))
    run_once(db)
