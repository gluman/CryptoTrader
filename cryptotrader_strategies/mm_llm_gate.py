#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
  mm_llm_gate.py — Composite MM LLM Gate (MiniMax-M3)
═══════════════════════════════════════════════════════════════════════════════

ЗАЧЕМ:
  Mode C (HYBRID) интеграция LLM в scan_execute pipeline.
  v7a rule-based pre-filter детектит spring/UTAD кандидатов (быстро, бесплатно).
  M3 LLM подтверждает/отклоняет каждого кандидата через Composite MM reasoning.

ЧТО ДЕЛАЕТ:
  1. Принимает decision dict от v7a.decide() (signal, indicators, details).
  2. Загружает composite_mm_master_prompt.md как system prompt.
  3. Вызывает MiniMax-M3 через Anthropic-compatible endpoint.
  4. Парсит JSON ответ, возвращает {confirm: bool, confidence, reasoning}.

ПОЧЕМУ MINIMAX-M3:
  - Тесты 18.06: 3/3 passed (FLAT→HOLD, SPRING→BUY, UTAD→SELL).
  - Latency 4-22s — приемлемо для 5m cron (~30 candidates/day).
  - Anthropic-compatible endpoint = минимальный код.

ОГРАНИЧЕНИЯ:
  - ~30 calls/day × 2188 tokens = 65K tokens/day = 2M tokens/month.
  - MiniMax key (len=125) читается raw bytes из ~/.hermes/.env (Hermes masks display).
  - При недоступности LLM → graceful fallback: v7a decision пропускает без LLM.

СМ. ТАКЖЕ:
  - composite_mm_master_prompt.md — master prompt
  - mm_strategy_rules.json — extracted rules (D3/E2/B4/N5)
  - skill: composite-mm-llm-pipeline
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# R20: pandas нужен для _compute_indicators_for_llm (BB, ATR, SMA вычисления)
import pandas as pd

log = logging.getLogger("mm_llm_gate")

# ═══ Конфигурация ═══
MASTER_PROMPT_PATH = "/home/andy/CryptoTrader_main/market_maker_library/composite_mm_master_prompt.md"
ENV_PATH = os.path.expanduser("~/.hermes/.env")
M3_ENDPOINT = "https://api.minimax.io/anthropic/v1/messages"
M3_MODEL = "MiniMax-M3"
M3_TIMEOUT = 60  # seconds

# Кешируем master prompt (читается один раз за процесс)
_cached_prompt: Optional[str] = None


def _compute_indicators_for_llm(df) -> Dict[str, Any]:
    """═══════════════════════════════════════════════════════════════════════════
    R20 (19.06.2026): Вычисление рыночных индикаторов из OHLCV для LLM primary.
    ════════════════════════════════════════════════════════════════════════════

    ЗАЧЕМ:
      v7a при HOLD не заполняет details индикаторами — LLM получает пустые данные.
      Эта функция вычисляет ключевые индикаторы НАПРЯМУЮ из DataFrame, чтобы LLM
      имела реальную рыночную картину независимо от v7a signal.

    ЧТО ВЫЧИСЛЯЕТ:
      - vol_spike: volume[-1] / mean(volume[-20:]) — всплеск объёма
      - bar_return_pct: return последнего бара
      - wick_ratio: (upper_wick + lower_wick) / body — отторгающий ли фитиль
      - bb_width_pct: Bollinger Band width (% от цены) — сжатие/расширение
      - atr_pct: ATR(14) / close — средняя волатильность
      - range_20_pct: (max-min последних 20 баров) / close — диапазон
      - close, high, low, volume последнего бара
      - trend_5_20: наклон (SMA5 vs SMA20) — восходящий/нисходящий

    Args:
        df: pandas DataFrame с OHLCV (columns: open, high, low, close, volume),
            DatetimeIndex, минимум 30 строк.

    Returns:
        Dict с индикаторами. Пустой dict при ошибке/недостаточно данных.
    ════════════════════════════════════════════════════════════════════════════
    """
    try:
        if df is None or len(df) < 30:
            return {}

        import numpy as np

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)
        opn = df["open"].astype(float)

        # Last bar
        c_last = float(close.iloc[-1])
        o_last = float(opn.iloc[-1])
        h_last = float(high.iloc[-1])
        l_last = float(low.iloc[-1])
        v_last = float(volume.iloc[-1])

        # Volume spike: last vs 20-bar mean
        vol_mean = float(volume.iloc[-21:-1].mean()) if len(volume) >= 21 else float(volume.mean())
        vol_spike = v_last / vol_mean if vol_mean > 0 else 1.0

        # Bar return %
        bar_return_pct = ((c_last - o_last) / o_last * 100) if o_last > 0 else 0.0

        # Wick ratio: total wick / body
        body = abs(c_last - o_last)
        upper_wick = h_last - max(c_last, o_last)
        lower_wick = min(c_last, o_last) - l_last
        total_wick = upper_wick + lower_wick
        wick_ratio = (total_wick / body) if body > 0 else 0.0

        # BB width (20, 2σ)
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bb_upper = sma20 + 2 * std20
        bb_lower = sma20 - 2 * std20
        bb_width = float(bb_upper.iloc[-1] - bb_lower.iloc[-1])
        bb_width_pct = (bb_width / c_last * 100) if c_last > 0 else 0.0

        # ATR(14) %
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr14 = tr.rolling(14).mean()
        atr_pct = (float(atr14.iloc[-1]) / c_last * 100) if c_last > 0 else 0.0

        # Range 20 bars %
        range_20 = float(high.iloc[-20:].max() - low.iloc[-20:].min())
        range_20_pct = (range_20 / c_last * 100) if c_last > 0 else 0.0

        # Trend: SMA5 vs SMA20
        sma5 = close.rolling(5).mean()
        trend_5_20 = "up" if float(sma5.iloc[-1]) > float(sma20.iloc[-1]) else "down"
        trend_strength = abs(float(sma5.iloc[-1]) - float(sma20.iloc[-1])) / c_last * 100 if c_last > 0 else 0.0

        # Lower wick rejection (bullish) — close in upper third of bar
        bar_range = h_last - l_last
        close_position = ((c_last - l_last) / bar_range) if bar_range > 0 else 0.5
        is_lower_wick_reject = (close_position > 0.6) and (lower_wick > body * 0.8) if body > 0 else False

        # Upper wick rejection (bearish) — close in lower third
        is_upper_wick_reject = (close_position < 0.4) and (upper_wick > body * 0.8) if body > 0 else False

        return {
            "close": round(c_last, 6),
            "vol_spike": round(vol_spike, 2),
            "vol_mean_20": round(vol_mean, 0),
            "vol_last": round(v_last, 0),
            "bar_return_pct": round(bar_return_pct, 3),
            "wick_ratio": round(wick_ratio, 2),
            "upper_wick_reject": is_upper_wick_reject,
            "lower_wick_reject": is_lower_wick_reject,
            "close_position": round(close_position, 2),
            "bb_width_pct": round(bb_width_pct, 3),
            "atr_pct": round(atr_pct, 3),
            "range_20_pct": round(range_20_pct, 3),
            "trend_5_20": trend_5_20,
            "trend_strength_pct": round(trend_strength, 3),
            "bars_analyzed": len(df),
        }
    except Exception as e:
        log.warning(f"_compute_indicators_for_llm error: {e}")
        return {}


def _load_master_prompt() -> str:
    """Загружает composite_mm_master_prompt.md. Кеширует."""
    global _cached_prompt
    if _cached_prompt is not None:
        return _cached_prompt
    try:
        _cached_prompt = Path(MASTER_PROMPT_PATH).read_text()
        return _cached_prompt
    except Exception as e:
        log.warning(f"master prompt load failed: {e}")
        _cached_prompt = "You are a Composite Operator. Analyze market data and return JSON with signal/confidence/reasoning."
        return _cached_prompt


def _load_minimax_key() -> Optional[str]:
    """Читает MINIMAX_API_KEY из ~/.hermes/.env raw bytes (обходит Hermes mask).

    Returns:
        API key string or None if not found / masked.
    """
    try:
        with open(ENV_PATH, "rb") as f:
            raw = f.read()
        for line in raw.split(b"\n"):
            if line.startswith(b"MINIMAX_API_KEY"):
                val = line.split(b"=", 1)[1].strip()
                if b"***" not in val:
                    return val.decode()
        return None
    except Exception as e:
        log.warning(f"minimax key load failed: {e}")
        return None


def _call_m3(system_prompt: str, user_content: str, timeout: int = M3_TIMEOUT) -> Optional[str]:
    """Вызывает MiniMax-M3 через Anthropic-compatible endpoint.

    Returns:
        Текст ответа или None при ошибке.
    """
    key = _load_minimax_key()
    if not key:
        log.warning("MINIMAX_API_KEY not available — LLM gate disabled")
        return None

    payload = {
        "model": M3_MODEL,
        "max_tokens": 2000,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        M3_ENDPOINT,
        data=data,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
        # Anthropic format: content = [{"type": "text", "text": "..."}]
        content_blocks = result.get("content", [])
        text = ""
        for block in content_blocks:
            if block.get("type") == "text":
                text += block.get("text", "")
        return text
    except urllib.error.HTTPError as e:
        log.warning(f"M3 HTTP {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        log.warning(f"M3 call failed: {e}")
        return None


def _parse_llm_response(text: str) -> Optional[Dict[str, Any]]:
    """Парсит JSON из ответа LLM. Толерантен к markdown wrappers и prose.

    ═══════════════════════════════════════════════════════════════════════════
    ПОЧЕМУ ЭТОТ ПАРСЕР ОБНОВЛЁН (27.06.2026 — P1 фикс, Толя)
    ═══════════════════════════════════════════════════════════════════════════
    ПРОБЛЕМА: M3 иногда возвращает JSON, обёрнутый в markdown (```json ... ```)
    с дополнительным prose-текстом после блока. Старый regex `\\{.*\\}` — жадный,
    захватывает лишнее. Также M3 иногда оборачивает JSON в одинарные backticks
    или добавляет комментарии до/после блока.

    СТАТИСТИКА (7 дней, 20-27.06): ~104 parse_failed из ~5000+ вызовов (~2%).
    Распределено по всем парам, не привязано к конкретной монете.

    ФИКС: Многоуровневый парсинг:
      1. Прямой json.loads (быстрый путь для идеальных ответов).
      2. Извлечение ```json ... ``` блока (markdown fenced).
      3. Извлечение первого валидного {...} блока (нежадный, по уровням скобок).
      4. Очистка trailing comma (частая ошибка LLM).
    ═══════════════════════════════════════════════════════════════════════════
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # Strategy 1: Direct JSON parse (fast path)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: Extract from markdown fenced block ```json ... ```
    md_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text, re.IGNORECASE)
    if md_match:
        candidate = md_match.group(1)
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            candidate = _fix_json_trailing_comma(candidate)
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                pass

    # Strategy 3: Extract first balanced { ... } block (non-greedy by brace depth)
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        candidate = _fix_json_trailing_comma(candidate)
                        try:
                            return json.loads(candidate)
                        except (json.JSONDecodeError, ValueError):
                            pass  # Try next balanced block

    # Strategy 4: Old regex fallback (greedy — last resort)
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidate = m.group(0)
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            candidate = _fix_json_trailing_comma(candidate)
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                pass

    return None


def _fix_json_trailing_comma(text: str) -> str:
    """Remove trailing commas before } or ] — common LLM mistake."""
    return re.sub(r',\s*([}\]])', r'\1', text)


def llm_primary_decision(
    symbol: str,
    timeframe: str,
    decision: Dict[str, Any],
    df: Any = None,
) -> Dict[str, Any]:
    """═══════════════════════════════════════════════════════════════════════════
    R20 (19.06.2026): LLM = PRIMARY decision-maker (Mode D).
    ════════════════════════════════════════════════════════════════════════════

    ЗАЧЕМ:
      Босс приказал сменить парадигму: LLM должна "следовать нашей стратегии" —
      быть активным decision-maker, а не пассивным post-filter (R18 Mode C).
      В high-vol regime M3 сама решает BUY/SELL/HOLD по Composite MM правилам,
      опираясь на индикаторы, которые v7a детектор поставляет как данные (не как
      фильтр). v7a score → модификатор уверенности, не жёсткий гейт.

    ЧТО ДЕЛАЕТ:
      1. Принимает decision dict от v7a.decide() (индикаторы + details).
         v7a signal/confidence передаются как REFERENCE — LLM может подтвердить
         или переопределить.
      2. Загружает composite_mm_master_prompt.md (5 принципов Composite Operator).
      3. Вызывает M3 через Anthropic-compatible endpoint.
      4. Возвращает решение LLM как PRIMARY: signal/side/confidence/sl/tp/reasoning.

    ОТЛИЧИЕ от llm_confirm_signal() (R18 Mode C):
      - Mode C: v7a первична, LLM = pass/reject гейт (confirmed bool).
      - Mode D: LLM первична, v7a = индикаторный input + reference.
      - agree/confirmed поля сохранены для совместимости с monitor/reporting,
        но confirmed теперь = (llm_signal != HOLD AND llm_confidence >= threshold),
        без требования совпадения с v7a.

    ПОРОГ ВХОДА:
      LLM решение исполняется если llm_signal ∈ {BUY,SELL} AND llm_confidence
      >= LLM_PRIMARY_MIN_CONF (по умолчанию 0.55 — выше R18 Mode C 0.50, т.к.
      LLM теперь несёт полную ответственность за решение).

    GRACEFUL FALLBACK:
      При недоступности LLM → confirmed=False, сигнал НЕ исполняется (safe mode).
      В отличие от Mode C (там fallback=pass-through, т.к. v7a trusted), здесь
      LLM primary → нет LLM = нет трейда. Безопаснее пропустить, чем торговать
      вслепую без primary decision-maker.

    СМ. ТАКЖЕ:
      - skill: composite-mm-llm-pipeline (раздел R20)
      - composite_mm_master_prompt.md — master prompt
      - clone5_multi_runner.py:scan_strategy() — вызов при vol_regime=high
    ════════════════════════════════════════════════════════════════════════════

    Args:
        symbol: торговая пара (SUIUSDT)
        timeframe: таймфрейм (5m)
        decision: dict из v7a.decide() — индикаторы + (возможно) v7a signal

    Returns:
        {
            "confirmed": bool,           # True = исполнять (LLM BUY/SELL + conf>=min)
            "llm_signal": str,           # BUY/SELL/HOLD — PRIMARY решение
            "llm_side": str,             # LONG/SHORT
            "llm_confidence": float,     # 0.0-1.0
            "llm_reasoning": str,        # reasoning Composite Operator
            "llm_sl_pct": float,         # SL% из LLM (если задала) или v7a fallback
            "llm_tp_pct": float,         # TP% из LLM или v7a fallback
            "co_action": str,            # accumulating/distributing/neutral
            "v7a_signal": str,           # для reference/audit (what v7a said)
            "agree": bool,               # LLM и v7a совпали по направлению
            "mode": "D_primary",         # режим для audit trail
            "error": str | None,
        }
    """
    # Порог уверенности для исполнения primary решения (R20)
    LLM_PRIMARY_MIN_CONF = 0.55

    v7a_signal = decision.get("signal", "HOLD")
    details = decision.get("details", {})

    # R20 FIX: v7a при HOLD не заполняет details индикаторами (только {'last': N}).
    # Вычисляем индикаторы НАПРЯМУЮ из DataFrame — LLM primary видит реальные данные.
    ind = _compute_indicators_for_llm(df) if df is not None else {}

    # Дополняем v7a-специфичными флагами (если они есть — при non-HOLD setup)
    for k in ("is_spring", "is_utad", "is_absorbing", "is_climax", "is_engulfing",
              "sweep_dist_pct", "ev_ratio", "eql_count", "near_round", "trend_alignment"):
        v = details.get(k)
        if v is not None and v != 0 and v is not False:
            ind[k] = v

    # vol_regime из strategy params (не из details)
    ind["vol_regime"] = details.get("vol_regime", "unknown")

    # Mode D prompt: LLM = primary, v7a = reference. Явно просим принять решение.
    user_msg = f"""You are the PRIMARY decision-maker for this trade. Analyze as the Composite Operator.

Symbol: {symbol}
Timeframe: {timeframe}

v7a RULE-BASED REFERENCE (indicator provider — verify or OVERRIDE):
  Signal: {v7a_signal}
  Confidence: {decision.get('confidence', 0):.2f}
  Side: {decision.get('side', 'unknown')}

INDICATORS:
{json.dumps(ind, indent=2)}

v7a REASONING: {decision.get('reasoning', '')}

You are PRIMARY. Do NOT just follow v7a — make your OWN decision using the 5 principles
(Composite Man, Liquidity Hunt, Effort vs Result, Mass Psychology, Statistical Edge).
Run the chain-of-thought checklist. You may agree or disagree with v7a.

Decide: BUY, SELL, or HOLD. If BUY/SELL, set stop_loss_pct and take_profit_pct.

═══════════════════════════════════════════════════════════════════════
FEE-AWARE RISK MANAGEMENT (R23, 08.07.2026)
═══════════════════════════════════════════════════════════════════════
Bybit linear perpetual fee = 0.055% per side × 2 = 0.11% round-trip.
On $15 notional this is ~$0.0165 PER trade. The constraints below
ensure NET profit/loss is REAL after fee, not gross:

  • Minimum stop_loss_pct: 1.20% (NET distance, ≥0.20% above gross
    for fee buffer). Smaller SL = noise trigger + fee eats everything.
  • Minimum take_profit_pct: 2.50% (NET profit). Smaller TP = gross
    < fee, NET loss.
  • Minimum risk:reward (NET): 2.0. With SL=1.20%, TP MUST be ≥ 2.40%.
  • Rationale: 1.2% SL net × 2.0 RR = 2.4% TP minimum. Everything
    below this threshold has negative expectancy at any WR.

Return your decision as JSON per the format specification."""

    system_prompt = _load_master_prompt()
    text = _call_m3(system_prompt, user_msg)

    if text is None:
        # Mode D graceful fallback: LLM primary недоступна → НЕТ трейда (safe).
        # В отличие от Mode C (pass-through), здесь LLM = primary, нет LLM = стоп.
        # R22 (08.07.2026): defaults 1.2/3.5 для fee-survival ($15 notional).
        return {
            "confirmed": False,
            "llm_signal": "HOLD",
            "llm_side": "NONE",
            "llm_confidence": 0.0,
            "llm_reasoning": "llm_unavailable_mode_d_safe_hold",
            "llm_sl_pct": decision.get("stop_loss_pct", 1.2),
            "llm_tp_pct": decision.get("take_profit_pct", 3.5),
            "co_action": "unknown",
            "v7a_signal": v7a_signal,
            "agree": False,
            "mode": "D_primary",
            "error": "llm_call_failed",
        }

    parsed = _parse_llm_response(text)
    if parsed is None:
        # R22 (08.07.2026): defaults 1.2/3.5 для fee-survival.
        return {
            "confirmed": False,
            "llm_signal": "HOLD",
            "llm_side": "NONE",
            "llm_confidence": 0.0,
            "llm_reasoning": "llm_parse_failed_mode_d_safe_hold",
            "llm_sl_pct": decision.get("stop_loss_pct", 1.2),
            "llm_tp_pct": decision.get("take_profit_pct", 3.5),
            "co_action": "unknown",
            "v7a_signal": v7a_signal,
            "agree": False,
            "mode": "D_primary",
            "error": "parse_failed",
        }

    llm_signal = parsed.get("signal", "HOLD")
    # FIX: confidence может прийти как None → float(None) = TypeError
    _raw_conf = parsed.get("confidence", 0)
    llm_conf = float(_raw_conf) if _raw_conf is not None else 0.0
    llm_reason = str(parsed.get("reasoning", ""))[:300]
    co_action = parsed.get("composite_operator_action", "unknown")

    # SL/TP: LLM может задать свои (или None при HOLD), иначе fallback на v7a.
    # R20 FIX: parsed.get может вернуть None (LLM: "stop_loss_pct": null при HOLD),
    # float(None) → TypeError. Фильтруем None перед float.
    # R22 (08.07.2026): defaults 1.2/3.5 для fee-survival ($15 notional).
    _raw_sl = parsed.get("stop_loss_pct")
    llm_sl_pct = float(_raw_sl) if _raw_sl is not None else decision.get("stop_loss_pct", 1.2)
    _raw_tp = parsed.get("take_profit_pct")
    llm_tp_pct = float(_raw_tp) if _raw_tp is not None else decision.get("take_profit_pct", 3.5)

    # Side из LLM решения
    if llm_signal == "BUY":
        llm_side = "LONG"
    elif llm_signal == "SELL":
        llm_side = "SHORT"
    else:
        llm_side = "NONE"

    # v7a side для сравнения
    v7a_side = decision.get("side", "LONG" if v7a_signal == "BUY" else "SHORT")

    # R23 (08.07.2026, Босс): SL floor с учётом fee-buffer.
    #   • MIN_SL_PCT 1.20% = minimum net-distance для входа. Реальный gross на бирже
    #     = 1.20% + 0.20% (fee_buffer) = 1.40%. Hit loss = 1.20% net (fee уже поглощён buffer).
    #   • MIN_TP_PCT 2.50% = minimum net-profit чтобы NET hit ≥ 2.39% (после fee).
    #   • Было 0.6/1.5 — fee съедало более 50% gross на маленьких TP.
    MIN_SL_PCT = 1.20
    MIN_TP_PCT = 2.50
    llm_sl_pct = max(llm_sl_pct, MIN_SL_PCT)
    llm_tp_pct = max(llm_tp_pct, MIN_TP_PCT)

    # Mode D: confirmed = LLM решила BUY/SELL с достаточной уверенностью.
    # Требование agree с v7a УБРАНО — LLM primary, может переопределить.
    confirmed = (llm_signal in ("BUY", "SELL")) and (llm_conf >= LLM_PRIMARY_MIN_CONF)

    # agree сохраняем для audit (согласны ли LLM и v7a по направлению)
    agree = (llm_signal == v7a_signal) and (
        (llm_signal == "HOLD") or (llm_side == v7a_side)
    )

    return {
        "confirmed": confirmed,
        "llm_signal": llm_signal,
        "llm_side": llm_side,
        "llm_confidence": llm_conf,
        "llm_reasoning": llm_reason,
        "llm_sl_pct": llm_sl_pct,
        "llm_tp_pct": llm_tp_pct,
        "co_action": co_action,
        "v7a_signal": v7a_signal,
        "agree": agree,
        "mode": "D_primary",
        "error": None,
    }


def llm_confirm_signal(
    symbol: str,
    timeframe: str,
    decision: Dict[str, Any],
) -> Dict[str, Any]:
    """LLM HYBRID gate: подтверждает или отклоняет v7a signal через M3.

    Args:
        symbol: торговая пара (SUIUSDT)
        timeframe: таймфрейм (5m)
        decision: dict из v7a.decide() с signal/confidence/details/sl/tp

    Returns:
        {
            "confirmed": bool,           # True = LLM подтверждает вход
            "llm_signal": str,           # BUY/SELL/HOLD из LLM
            "llm_confidence": float,     # 0.0-1.0
            "llm_reasoning": str,        # короткий reasoning
            "co_action": str,            # accumulating/distributing/neutral
            "agree": bool,               # LLM и v7a согласны по направлению
            "error": str | None,         # ошибка если LLM недоступен
        }

    Если LLM недоступен → confirmed=True (пропускаем v7a signal без LLM проверки,
    graceful degradation — лучше исполнить чем пропустить).
    """
    v7a_signal = decision.get("signal", "HOLD")
    details = decision.get("details", {})
    ind = {
        "close": details.get("close", 0),
        "vol_spike": details.get("vol_spike", 0),
        "sweep_dist_pct": details.get("sweep_dist_pct", 0),
        "wick_ratio": details.get("wick_ratio", 0),
        "ev_ratio": details.get("ev_ratio", 0),
        "is_absorbing": details.get("is_absorbing", False),
        "is_climax": details.get("is_climax", False),
        "is_spring": details.get("is_spring", False),
        "is_utad": details.get("is_utad", False),
        "eql_count": details.get("eql_count", details.get("eqh_count", 0)),
        "near_round": details.get("near_round", False),
        "hour_utc": details.get("hour_utc", 0),
        "vol_regime": details.get("vol_regime", "unknown"),
        "bb_width_pct": details.get("vol_bb_width", 0),
        # R18 gap-filter info для LLM context
        "bar_return_pct": details.get("bar_return_pct", 0),
        "trend_alignment": details.get("trend_alignment", "neutral"),
        "is_engulfing": details.get("is_engulfing", False),
    }

    user_msg = f"""Analyze this market data as the Composite Operator.

Symbol: {symbol}
Timeframe: {timeframe}

v7a RULE-BASED DETECTION (for reference — verify or override):
  Signal: {v7a_signal}
  Confidence: {decision.get('confidence', 0):.2f}
  Side: {decision.get('side', 'unknown')}
  SL: {decision.get('stop_loss_pct', 0):.2f}%
  TP: {decision.get('take_profit_pct', 0):.2f}%

INDICATORS:
{json.dumps(ind, indent=2)}

V7A REASONING: {decision.get('reasoning', '')}

Apply the 5 principles (Composite Man, Liquidity Hunt, Effort vs Result, Mass Psychology, Statistical Edge).
Run the chain-of-thought checklist.
Return your decision as JSON per the format specification."""

    system_prompt = _load_master_prompt()
    text = _call_m3(system_prompt, user_msg)

    if text is None:
        # Graceful fallback: пропускаем signal без LLM
        return {
            "confirmed": True,
            "llm_signal": v7a_signal,
            "llm_confidence": 0.0,
            "llm_reasoning": "llm_unavailable_graceful_fallback",
            "co_action": "unknown",
            "agree": True,
            "error": "llm_call_failed",
        }

    parsed = _parse_llm_response(text)
    if parsed is None:
        return {
            "confirmed": True,
            "llm_signal": v7a_signal,
            "llm_confidence": 0.0,
            "llm_reasoning": "llm_parse_failed",
            "co_action": "unknown",
            "agree": True,
            "error": "parse_failed",
        }

    llm_signal = parsed.get("signal", "HOLD")
    # FIX: confidence может прийти как None → float(None) = TypeError
    _raw_conf = parsed.get("confidence", 0)
    llm_conf = float(_raw_conf) if _raw_conf is not None else 0.0
    llm_reason = str(parsed.get("reasoning", ""))[:300]
    co_action = parsed.get("composite_operator_action", "unknown")

    # LLM подтверждает если:
    # 1. LLM signal совпадает с v7a по направлению (agree)
    # 2. LLM confidence >= 0.50 (не отклоняет явно)
    v7a_side = decision.get("side", "LONG" if v7a_signal == "BUY" else "SHORT")
    llm_side = parsed.get("side", "LONG" if llm_signal == "BUY" else "SHORT")
    agree = (llm_signal == v7a_signal) and (llm_side == v7a_side)
    confirmed = agree and llm_conf >= 0.50

    return {
        "confirmed": confirmed,
        "llm_signal": llm_signal,
        "llm_confidence": llm_conf,
        "llm_reasoning": llm_reason,
        "co_action": co_action,
        "agree": agree,
        "error": None,
    }
