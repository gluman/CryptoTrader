#!/usr/bin/env python3
"""LLM-фильтр рыночного режима: раз в 6 часов решает, какие пары торговать.

═══════════════════════════════════════════════════════════════════════════════
ЗАЧЕМ И НА ЧЁМ ОСНОВАНО
═══════════════════════════════════════════════════════════════════════════════
Механика v9 CTL даёт ~1650 сигналов за 90 дней, но лимит в 3 одновременные
позиции пропускает лишь ~480 из них, причём очередь берёт худшие входы (exp
исполненных +0.05% против +0.28% у отброшенных). Этот фильтр убирает часть пар
ДО очереди, чтобы дефицитные слоты доставались лучшим сигналам.

Замер на 90 днях (358 точек × 26 пар, MiniMax-M3, контекст строго до момента
оценки, скрипты в scratchpad сессии 271ab5d3):

    все сигналы        1654   exp +0.251%
    LLM разрешила      1235   exp +0.311%     ← 75%
    LLM заблокировала   419   exp +0.073%     ← 25%

  портфельно (лимит 3, $5, дневной стоп −3.38$):
    v9 как есть        477 сделок  +1.25$  просадка −2.72$
    v9 + этот фильтр   350 сделок  +2.21$  просадка −2.24$

  bootstrap: лучше случайного отбора той же доли в 97% симуляций.

⚠️ ЧЕСТНЫЕ ОГОВОРКИ (решение включить принято Боссом 13.08.2026 с их учётом):
  • значимость на грани: t=1.83 для разницы разрешённые−заблокированные;
  • помесячно нестабильно: +0.59 / +1.27 / −0.90$ — худший последний месяц;
  • окно 6ч не подбиралось (подбор окна = переобучение);
  • без портфельных лимитов фильтр, наоборот, чуть хуже (+19.22$ против
    +20.76$) — весь выигрыш идёт от перераспределения слотов.

Проверено, что это НЕ замаскированное правило по тренду: LLM блокирует в
основном пары с трендом up, но механическая замена работает хуже, чем полное
отсутствие фильтра (правило «тренд=down» +0.70$, «тренд не up» +0.94$ против
+1.25$ без фильтра). Сверх правила модель блокирует 355 сигналов при тренде
down с exp −0.033% — отсекает убыточную часть внутри годного режима.

═══════════════════════════════════════════════════════════════════════════════
ПОВЕДЕНИЕ
═══════════════════════════════════════════════════════════════════════════════
  • окна привязаны к UTC: 00:00, 06:00, 12:00, 18:00 — как в замере;
  • один батч-запрос на окно по всем парам (≈4 вызова в сутки);
  • FAIL-OPEN: любая ошибка (нет ключа, таймаут, кривой JSON, нет вердикта по
    паре) → торгуем пару как раньше. Фильтр может только УБАВИТЬ сделки, но
    никогда не остановит торговлю целиком;
  • вердикты и причины пишутся в JSON-кэш — по нему потом сверяем предсказание
    с фактическим исходом.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

WINDOW_HOURS = 6
CACHE_PATH = Path("/home/andy/CryptoTrader/config/llm_regime_cache.json")
HISTORY_PATH = Path("/home/andy/CryptoTrader/logs/llm_regime_history.jsonl")
M3_ENDPOINT = "https://api.minimax.io/anthropic/v1/messages"
M3_MODEL = "MiniMax-M3"
TIMEOUT = 90

SYSTEM = """You are a market-regime gate for a crypto scalping bot.

The bot runs ONE mechanical setup: counter-trend long after a swing-low sweep
(stop-hunt) while the 1h trend is down. It wins ~58% of the time on average and is
profitable overall. It only trades between 08:00 and 20:00 UTC.

For each symbol you decide whether this setup should be ALLOWED for the next few
hours, based on the market regime of that symbol.

ALLOW when mean-reversion is likely to work: orderly downtrend, normal or elevated
volatility, dips getting bought.
AVOID when the setup is likely to break down: violent one-way liquidation with no
bounces, volatility collapse where price cannot reach the take-profit, or a strong
vertical trend that keeps running.

Do NOT avoid a symbol merely because the trend is down — a down 1h trend is a
REQUIREMENT of this setup, not a warning sign.

Calibration: allow roughly two thirds of symbols. Blocking everything destroys a
profitable strategy; blocking nothing is useless.

Reply with STRICT JSON only, one line, no prose:
{"SYMBOL":{"d":"TRADE"|"AVOID","c":0.0-1.0}, ...} for every symbol given."""


def current_window() -> int:
    """Метка текущего 6-часового окна в мс (UTC, кратно WINDOW_HOURS)."""
    now = datetime.now(timezone.utc)
    h = (now.hour // WINDOW_HOURS) * WINDOW_HOURS
    w = now.replace(hour=h, minute=0, second=0, microsecond=0)
    return int(w.timestamp() * 1000)


def _load_cache() -> Dict[str, Any]:
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(data: Dict[str, Any]) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    except Exception as e:
        log.warning(f"llm_regime_gate: cache write failed: {e}")


def _minimax_key() -> Optional[str]:
    """Ключ читается из ~/.hermes/.env raw-байтами — так же, как в mm_llm_gate."""
    try:
        raw = Path("/home/andy/.hermes/.env").read_bytes()
        m = re.search(rb"^MINIMAX_API_KEY=(.+)$", raw, re.M)
        if m:
            return m.group(1).decode().strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def build_snapshot(symbol: str, df: pd.DataFrame, trend: str) -> Optional[str]:
    """Строка состояния пары. Формат ДОЛЖЕН совпадать с тем, на котором мерили."""
    try:
        if df is None or len(df) < 150:
            return None
        c = df["close"].to_numpy(dtype=float)
        h = df["high"].to_numpy(dtype=float)
        l = df["low"].to_numpy(dtype=float)
        v = df["volume"].to_numpy(dtype=float)
        px = float(c[-1])
        if px <= 0:
            return None
        ret6 = (px / c[-72] - 1) * 100 if len(c) > 72 else 0.0
        ret24 = (px / c[-288] - 1) * 100 if len(c) > 288 else 0.0
        atr = float(pd.Series(h[-48:] - l[-48:]).mean() / px * 100)
        atr_prev = (float(pd.Series(h[-144:-48] - l[-144:-48]).mean() / px * 100)
                    if len(h) > 144 else atr)
        vol_now = float(v[-72:].mean())
        vol_prev = float(v[-288:-72].mean()) if len(v) > 288 else vol_now
        vol_ratio = vol_now / vol_prev if vol_prev else 1.0
        d = pd.Series(c[-200:]).diff()
        up = d.clip(lower=0).rolling(14).mean().iloc[-1]
        dn = (-d.clip(upper=0)).rolling(14).mean().iloc[-1]
        rsi = float(100 - 100 / (1 + up / dn)) if dn and dn > 0 else 50.0
        lo24, hi24 = float(l[-288:].min()), float(h[-288:].max())
        pos = (px - lo24) / (hi24 - lo24) * 100 if hi24 > lo24 else 50.0
        return (f"{symbol}: 6h {ret6:+.1f}% | 24h {ret24:+.1f}% | ATR48 {atr:.2f}% "
                f"(prev {atr_prev:.2f}%) | vol {vol_ratio:.2f}x | RSI {rsi:.0f} | "
                f"pos in 24h range {pos:.0f}% | 1h trend {trend}")
    except Exception as e:
        log.warning(f"llm_regime_gate: snapshot({symbol}) failed: {e}")
        return None


def _ask(user: str, retries: int = 2) -> Optional[Dict[str, Any]]:
    key = _minimax_key()
    if not key:
        log.warning("llm_regime_gate: MINIMAX_API_KEY not available — fail-open")
        return None
    import requests
    payload = {"model": M3_MODEL, "max_tokens": 1500, "system": SYSTEM,
               "messages": [{"role": "user", "content": user}]}
    for a in range(retries + 1):
        try:
            r = requests.post(M3_ENDPOINT, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"}, json=payload, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(5 * (a + 1))
                continue
            if r.status_code != 200:
                log.warning(f"llm_regime_gate: HTTP {r.status_code}")
                time.sleep(2)
                continue
            txt = "".join(b.get("text", "") for b in r.json().get("content", [])
                          if b.get("type") == "text")
            m = re.search(r"\{.*\}", txt, re.S)
            if not m:
                continue
            return json.loads(m.group(0))
        except Exception as e:
            log.warning(f"llm_regime_gate: call failed: {e}")
            time.sleep(2 * (a + 1))
    return None


def ensure_verdicts(symbols, get_df: Callable[[str], pd.DataFrame],
                    trends: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Возвращает вердикты на текущее окно, обновляя их раз в WINDOW_HOURS.

    get_df(symbol) — загрузчик баров 5m (в бою это get_ohlcv из runner).
    Любая ошибка → пустой словарь, а он означает «фильтр не мешает» (fail-open).
    """
    cache = _load_cache()
    w = current_window()
    if cache.get("window") == w and isinstance(cache.get("verdicts"), dict):
        return {k: str(v.get("d", "TRADE")).upper() if isinstance(v, dict) else str(v).upper()
                for k, v in cache["verdicts"].items()}

    lines = []
    for sym in symbols:
        try:
            df = get_df(sym)
        except Exception:
            continue
        s = build_snapshot(sym, df, (trends or {}).get(sym, "unknown"))
        if s:
            lines.append("  " + s)
    if not lines:
        log.warning("llm_regime_gate: no snapshots — fail-open")
        return {}

    user = ("Current market snapshot (no dates given on purpose).\n"
            f"For each symbol decide TRADE or AVOID for the next {WINDOW_HOURS} hours.\n\n"
            + "\n".join(lines))
    raw = _ask(user)
    if not raw:
        log.warning("llm_regime_gate: LLM unavailable — fail-open for this window")
        return {}

    verdicts = {}
    for sym, x in raw.items():
        if isinstance(x, dict):
            verdicts[sym.upper()] = {"d": str(x.get("d", "TRADE")).upper(),
                                     "c": float(x.get("c", 0) or 0)}
        else:
            verdicts[sym.upper()] = {"d": str(x).upper(), "c": 0.0}

    _save_cache({"window": w,
                 "window_utc": datetime.fromtimestamp(w / 1000, timezone.utc).isoformat(),
                 "model": M3_MODEL, "verdicts": verdicts})
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open("a") as f:
            f.write(json.dumps({"window": w, "verdicts": verdicts}, ensure_ascii=False) + "\n")
    except Exception:
        pass

    avoid = sum(1 for v in verdicts.values() if v["d"] == "AVOID")
    log.info(f"llm_regime_gate: окно обновлено, AVOID {avoid}/{len(verdicts)}")
    return {k: v["d"] for k, v in verdicts.items()}


def is_allowed(symbol: str, verdicts: Dict[str, str]) -> bool:
    """FAIL-OPEN: нет вердикта — торгуем."""
    return verdicts.get(symbol.upper(), "TRADE") != "AVOID"
