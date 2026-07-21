#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════════
aggregate_pnl_manager.py — СУММАРНЫЙ PnL-КОНТРОЛЬ ПО ОТКРЫТЫМ ПОЗИЦИЯМ

ЗАЧЕМ
─────
Пользователь определил правило управления суммарным PnL всех открытых linear-позиций:
  • SUMMAR_PNL_BREAKEVEN_THRESHOLD_USDT (по умолчанию +9999, ОТКЛЮЧЕНО)
      → если суммарный uPnL ≥ этого порога — перенести SL каждой позиции
        в безубыток (entry). 2026-07-07 по приказу Босса перенос в BE убран;
        default и .env override выставлены в 9999, чтобы action
        `move_to_breakeven` был математически недостижим.
  • SUMMAR_PNL_TP_THRESHOLD_USDT (по умолчанию +1.0)
      → если суммарный uPnL ≥ этого порога — закрыть ВСЕ позиции по рынку
        (фиксация прибыли)
  • SUMMAR_PNL_SL_THRESHOLD_USDT (по умолчанию −1.0)
      → если суммарный uPnL ≤ этого порога — закрыть ВСЕ позиции по рынку
        (ограничение убытков)

Правила управляются env-переменными (см. defaults в коде). При желании
переопределить — задать в .env. Чтобы ВКЛЮЧИТЬ BE обратно — вернуть
SUMMAR_PNL_BREAKEVEN_THRESHOLD_USDT к реальному значению (например 0.5).

ЧТО
───
1. Подключение к Bybit через ccxt (private endpoint v5).
2. fetch_positions → список открытых linear-USDT позиций.
3. Сумма uPnL = Σ unrealisedPnl.
4. Логика:
     if total_pnl >= TP_THRESHOLD → закрыть все по рынку (Market Sell/Buy reduce_only)
     elif total_pnl >= BREAKEVEN_THRESHOLD → переставить SL в entry + ε
     elif total_pnl <= SL_THRESHOLD → закрыть все по рынку
     else → оставить как есть
5. Идемпотентность:
     • Если SL уже ≤ entry для long (или ≥ entry для short) — пропускаем
     • Если позиция уже упала ниже SL для long (SL < lastPrice) — не правим,
       иначе биржа вернёт 10001
6. Учёт комиссий: при перестановке SL используем entry + fee_buffer (по умолчанию 0.1%
   от entry), чтобы покрыть round-trip fee (~0.2%). Для заведомо прибыльных
   позиций — ставим entry + 0.0001 (точно break-even).

ПОЧЕМУ
─────
• Закрытие "по uPnL" по одной позиции не работает в много-парном портфеле:
  LIT может в плюсе +1$, SUI — в минусе -0.5$. Индивидуальный SL выбьет SUI
  и съест прибыль LIT. Агрегатный подход этого избегает.
• Market close использован потому что reduce-only market гарантированно закроет
  и при высокой волатильности; лимитный ордер может не сработать и позиция
  "зависнет".

ПРОБЛЕМЫ
────────
• Doge может сработать по SL сразу после выставления (см. пример 2026-07-06):
  цена дошла до lastPrice*0.9995 в течение секунд. Это нормально, не ошибка.
• Bybit V5 требует SL < lastPrice для Buy и SL > lastPrice для Sell.
  Если entry уже "хуже" рынка — ставим SL на lastPrice±ε (это worst-case).
• fetch_positions возвращает SL через float; если SL уже на бирже = 0 (т.е.
  не задан), это None — обработать корректно.
• При закрытии "всех" не получится закрыть short через Market Sell reduce-only,
  нужен Market Buy reduce-only. Обработано.

СМ. ТАКЖЕ
─────────
• /home/andy/CryptoTrader_main/cryptotrader_strategies/clone5_multi_runner.py —
  R20 cron scanner, вызывает этот модуль в фазе post-scanner guard.
• ~/.hermes/cron/output/<id>/*.md — cron-отчёты; данный модуль идемпотентен,
  можно дёргать на каждом tick.

ИСПОЛЬЗОВАНИЕ
─────────────
python aggregate_pnl_manager.py                  # one-shot run
python aggregate_pnl_manager.py --dry-run        # показать что сделал бы, без side-effects
"""

from __future__ import annotations
import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import List, Optional, Tuple

import ccxt

# ═══════════════════════════════════════════════════════════════════════════════
# ЗАЧЕМ: модуль запускается двумя способами:
#   1. Через scan_and_execute_tg.sh (cron) — там load_dotenv уже вызван
#   2. Напрямую CLI (--dry-run, manual test) — без явной загрузки .env
# В обоих случаях defaults вступают в силу без явного override.
# Безопасный try/except import — если нет dotenv, полагаемся на системные env.
# ═══════════════════════════════════════════════════════════════════════════════
try:
    from dotenv import load_dotenv as _ld
    # Подгружаем стандартный .env если он есть рядом со скриптом
    _env_path = "/home/andy/CryptoTrader_main/.env"
    if os.path.isfile(_env_path):
        _ld(_env_path, override=False)
except ImportError:
    pass  # dotenv не доступен — используем sys env / defaults

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG (defaults; переопределяются через env)
# ═══════════════════════════════════════════════════════════════════════════════

DEF_TP = float(os.getenv("SUMMAR_PNL_TP_THRESHOLD_USDT", "1.0"))            # ≥ → close all (still active)
DEF_BE = float(os.getenv("SUMMAR_PNL_BREAKEVEN_THRESHOLD_USDT", "9999"))    # ≥ → move SL to BE  (DISABLED 2026-07-07; unreachable)
DEF_SL = float(os.getenv("SUMMAR_PNL_SL_THRESHOLD_USDT", "-9999"))          # ≤ → close all    (DISABLED 2026-07-07; unreachable)
FEE_BUFFER_PCT = float(os.getenv("SUMMAR_PNL_FEE_BUFFER_PCT", "0.20"))     # 0.20% сверх entry — R23: было 0.05, fee round-trip 0.11% не покрывал
SL_MARGIN_PCT = float(os.getenv("SUMMAR_PNL_SL_MARGIN_PCT", "0.05"))       # 0.05% от lastPrice

# Bybit API endpoint
BYBIT_BASE = os.getenv("BYBIT_API_BASE", "https://api.bybit.com")

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

log = logging.getLogger("aggregate_pnl")
log.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)


@dataclass
class Position:
    """Снимок открытой позиции с биржи."""
    symbol: str            # ccxt: "SUI/USDT:USDT"
    bybit_symbol: str      # "SUIUSDT"
    side: str              # 'long' / 'short'
    size: float
    entry: float
    mark: float
    upnl: float
    sl: Optional[float]

    def __repr__(self) -> str:
        return (f"Pos({self.symbol} {self.side} size={self.size:.4f} "
                f"entry={self.entry:.4f} mark={self.mark:.4f} "
                f"uPnL={self.upnl:+.4f} sl={self.sl})")


# ═══════════════════════════════════════════════════════════════════════════════
# BYBIT CONNECTION
# ═══════════════════════════════════════════════════════════════════════════════

def load_env(path: str = "/home/andy/CryptoTrader_main/.env") -> dict:
    """Минимальный парсер .env (без зависимостей)."""
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def make_exchange() -> ccxt.bybit:
    """Создаёт ccxt.bybit через общий bybit_safe.bybit_exchange().

    ═══ FIX 21.07.2026 (Босс: aggregate_pnl падал 10010 Unmatched IP каждые 5м) ═══
    Раньше брал BYBIT_API_KEY из .env НАПРЯМУЮ. Это VPN_OFF-ключ, привязанный к
    другому IP → Bybit возвращал retCode 10010 «Unmatched IP» на КАЖДОМ запуске,
    и менеджер совокупного PnL (BE/TP/SL) фактически не работал.
    Живой пайплайн (scan_and_execute) ходит через bybit_key_selector, который
    try-init'ит оба VPN-ключа и берёт рабочий. Делегируем туда же — один путь
    аутентификации на всю систему. См. bybit_safe.py / src.core.bybit_key_selector.
    """
    # Скрипт запускается как `python cryptotrader_strategies/aggregate_pnl_manager.py`
    # → sys.path[0] = папка скрипта, пакет cryptotrader_strategies не виден.
    # Добавляем корень проекта (как это делает scan_and_execute.py:99).
    _root = "/home/andy/CryptoTrader"
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from cryptotrader_strategies.bybit_safe import bybit_exchange
    return bybit_exchange(with_auth=True)


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_open_positions(ex: ccxt.bybit) -> List[Position]:
    """Загружает открытые linear-USDT позиции с биржи."""
    raw = ex.fetch_positions(params={"category": "linear", "settleCoin": "USDT"})
    out: List[Position] = []
    for p in raw:
        size = float(p.get("contracts") or 0)
        if size <= 0:
            continue
        sym = p.get("symbol", "")
        bybit_sym = sym.replace("/USDT:USDT", "USDT")
        entry = float(p.get("entryPrice") or p.get("info", {}).get("avgPrice") or 0)
        mark = float(p.get("markPrice") or 0)
        upnl = float(p.get("unrealizedPnl") or p.get("info", {}).get("unrealisedPnl") or 0)
        sl_raw = p.get("stopLoss") or p.get("info", {}).get("stopLoss")
        sl = float(sl_raw) if sl_raw and sl_raw not in ("0", "0.0", "") else None
        side = p.get("side", "")
        if side not in ("long", "short"):
            side = "long" if str(p.get("info", {}).get("side", "")).lower().startswith("buy") else "short"
        out.append(Position(
            symbol=sym,
            bybit_symbol=bybit_sym,
            side=side,
            size=size,
            entry=entry,
            mark=mark,
            upnl=upnl,
            sl=sl,
        ))
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# DECISION LOGIC
# ═══════════════════════════════════════════════════════════════════════════════

def compute_action(
    positions: List[Position],
    tp_threshold: float,
    be_threshold: float,
    sl_threshold: float,
) -> Tuple[str, dict]:
    """Возвращает ('close_all' | 'move_to_breakeven' | 'hold', info dict)."""
    total_upnl = sum(p.upnl for p in positions)
    info = {
        "total_upnl": round(total_upnl, 6),
        "tp_threshold": tp_threshold,
        "be_threshold": be_threshold,
        "sl_threshold": sl_threshold,
        "open_positions": len(positions),
    }
    if total_upnl >= tp_threshold:
        return "close_all", {**info, "reason": f"uPnL {total_upnl:+.4f} ≥ TP {tp_threshold}"}
    if total_upnl <= sl_threshold:
        return "close_all", {**info, "reason": f"uPnL {total_upnl:+.4f} ≤ SL {sl_threshold}"}
    if total_upnl >= be_threshold:
        return "move_to_breakeven", {**info, "reason": f"uPnL {total_upnl:+.4f} ≥ BE {be_threshold}"}
    return "hold", {**info, "reason": f"uPnL {total_upnl:+.4f} ∈ [{sl_threshold}, {be_threshold})"}


# ═══════════════════════════════════════════════════════════════════════════════
# SL COMPUTATION — приоритет: entry+fee, иначе lastPrice±ε
# ═══════════════════════════════════════════════════════════════════════════════

def target_sl_for_position(p: Position) -> float:
    """Вычисляет целевую SL-цену для позиции, чтобы при срабатывании
    совокупный PnL всех открытых был ≥ 0 (агрегатная безубыточность).

    Логика:
      • Для long: SL < mark обязательно (Bybit V5: 10001 иначе).
      • Берём entry + fee_buffer (по умолчанию +0.05%); если это всё ещё < mark — отлично.
      • Если entry + fee_buffer ≥ mark (т.е. позиция глубоко в минусе) —
        ставим SL = mark × (1 − margin).
    """
    fee = FEE_BUFFER_PCT / 100.0
    margin = SL_MARGIN_PCT / 100.0
    if p.side == "long":
        ideal = p.entry * (1.0 + fee)
        if ideal < p.mark:
            return _round_tick(ideal, p.mark, "down")
        # entry+fee >= mark → позиция глубоко в минусе; SL чуть ниже mark
        return _round_tick(p.mark * (1.0 - margin), p.mark, "down")
    # short
    ideal = p.entry * (1.0 - fee)
    if ideal > p.mark:
        return _round_tick(ideal, p.mark, "up")
    return _round_tick(p.mark * (1.0 + margin), p.mark, "up")


def _round_tick(price: float, ref: float, direction: str) -> float:
    """Округляем к tick (4 знака для альтов, 5 для крупных). Используем
    цену-референс для определения точности."""
    s = f"{ref:.8f}".rstrip("0")
    if "." in s:
        decimals = len(s.split(".")[1])
    else:
        decimals = 4
    # Clamp to safe range
    decimals = max(2, min(decimals, 6))
    quant = Decimal("1").scaleb(-decimals)  # 10^-decimals
    d = Decimal(str(price))
    if direction == "down":
        d = d.quantize(quant, rounding=ROUND_DOWN)
    else:
        d = d.quantize(quant, rounding=ROUND_UP)
    return float(d)


# ═══════════════════════════════════════════════════════════════════════════════
# BYBIT V5 OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def set_position_sl(ex: ccxt.bybit, p: Position, sl_price: float) -> dict:
    """Устанавливает SL на позицию через V5 trading-stop endpoint."""
    params = {
        "category": "linear",
        "symbol": p.bybit_symbol,
        "stopLoss": str(sl_price),
    }
    return ex.private_post_v5_position_trading_stop(params)


def close_position_market(ex: ccxt.bybit, p: Position) -> dict:
    """Закрывает позицию market reduce-only."""
    side_opposite = "sell" if p.side == "long" else "buy"
    params = {
        "category": "linear",
        "symbol": p.bybit_symbol,
        "side": side_opposite.capitalize(),
        "orderType": "Market",
        "qty": str(p.size),
        "reduceOnly": True,
        "closeOnTrigger": False,
    }
    return ex.private_post_v5_order_create(params)


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════════════

def manage(dry_run: bool = False) -> int:
    """Главная функция. Возвращает exit code (0 = ok, 1 = partial fail)."""
    ex = make_exchange()
    ex.load_time_difference()

    positions = fetch_open_positions(ex)
    if not positions:
        log.info("no open positions")
        return 0

    total_upnl = sum(p.upnl for p in positions)
    log.info(f"open={len(positions)} total_uPnL={total_upnl:+.4f} USDT")
    for p in positions:
        log.info(f"  {p}")

    action, info = compute_action(
        positions,
        tp_threshold=DEF_TP,
        be_threshold=DEF_BE,
        sl_threshold=DEF_SL,
    )
    log.info(f"ACTION={action} reason={info['reason']}")

    if dry_run:
        log.info("--dry-run: no changes will be sent")
        if action == "move_to_breakeven":
            for p in positions:
                tgt = target_sl_for_position(p)
                log.info(f"  would set {p.bybit_symbol} SL={tgt:.6f} (current={p.sl})")
        elif action == "close_all":
            for p in positions:
                log.info(f"  would MARKET CLOSE {p.bybit_symbol} {p.side} size={p.size}")
        return 0

    failures = 0
    if action == "close_all":
        for p in positions:
            try:
                log.info(f"CLOSE {p.bybit_symbol} {p.side} size={p.size}")
                resp = close_position_market(ex, p)
                log.info(f"  → {resp.get('retCode')} {resp.get('retMsg')}")
            except Exception as e:
                log.error(f"  close failed: {e}")
                failures += 1
        return 0 if failures == 0 else 1

    if action == "move_to_breakeven":
        for p in positions:
            tgt = target_sl_for_position(p)
            # Идемпотентность: если текущий SL уже близок к цели — пропускаем
            if p.sl is not None and abs(p.sl - tgt) / max(p.sl, tgt) < 1e-4:
                log.info(f"  {p.bybit_symbol} SL already at {p.sl} (target {tgt:.6f}); skip")
                continue
            try:
                log.info(f"  SET SL {p.bybit_symbol} → {tgt:.6f} (current {p.sl})")
                resp = set_position_sl(ex, p, tgt)
                rc = resp.get("retCode")
                rm = resp.get("retMsg")
                log.info(f"    → retCode={rc} retMsg={rm}")
                if int(rc or 0) not in (0,):
                    # 34040 = not modified (accept as ok if value matches)
                    if rc == "34040":
                        continue
                    failures += 1
            except Exception as e:
                log.error(f"  set SL failed for {p.bybit_symbol}: {e}")
                failures += 1
        return 0 if failures == 0 else 1

    log.info("HOLD: no changes")
    return 0


def main():
    ap = argparse.ArgumentParser(description="aggregate PnL manager for Bybit linear positions")
    ap.add_argument("--dry-run", action="store_true", help="only print what would be done")
    args = ap.parse_args()
    sys.exit(manage(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
