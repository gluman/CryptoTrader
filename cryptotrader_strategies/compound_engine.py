#!/usr/bin/env python3
"""
Compound Engine v1 — per-trade auto-reinvest.

Использование:
    python compound_engine.py --status      # показать текущий баланс
    python compound_engine.py --simulate    # симуляция compound на v7 OOS trades
    python compound_engine.py --rebalance   # обновить POS_USDT в live runner
    python compound_engine.py --rebalance --force  # даже если дельта < $0.50

Логика:
  - Текущий баланс Bybit
  - Размер позиции = balance / MAX_CONCURRENT (2)
  - Floor до $1, cap до $30 (per-pair exposure cap)
  - При балансе < $20 → POS_USDT = 5 (default)
  - При балансе > $50 → POS_USDT = 10
  - При балансе > $200 → POS_USDT = 15
"""
import os, sys, json, argparse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/andy/CryptoTrader_main')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader_main/.env')

import ccxt

# ═══ Правила масштабирования (06.08.2026, приказ Босса) ══════════════════════
#
# СНАЧАЛА РАСТЁТ ЧИСЛО ПОЗИЦИЙ, ПОТОМ РАЗМЕР СДЕЛКИ.
#
# Почему такой порядок — замер v9 CTL на 90 днях, депозит $100:
#   $25 × 4 позиции   →  +6.9%,  просадка 15.2%
#   $20 × 5 позиций   →  +8.4%,  просадка 13.5%
#   $12 × 8 позиций   →  +7.8%,  просадка 10.8%
#   $10 × 10 позиций  → +11.3%,  просадка  7.1%
#   $8  × 12 позиций  → +11.4%,  просадка  5.3%   ← лучший результат
#   $6  × 15 позиций  →  +9.2%,  просадка  3.9%   ← лучший риск/доходность
# Много мелких позиций выигрывают у немногих крупных сразу по обоим параметрам:
# сделки расходятся по разным парам и моментам, и один неудачный вход перестаёт
# определять итог. Обратный порядок (растить размер при 3 позициях) давал вдвое
# меньше прибыли при вдвое большей просадке.
#
# Прежние TIERS (5/7/10/12/15/20/25 по балансу) растили ТОЛЬКО размер и удерживали
# число позиций на 3 — то есть ровно худший из проверенных вариантов. Удалены.
#
# Формула (линейная):
#   usable = free_balance × CAPITAL_USE          — часть капитала под работу
#   n = usable / MIN_POS_USD                     — сколько влезает по минимуму
#   n <= MAX_POSITIONS →  позиций n,           размер MIN_POS_USD  (фаза 1)
#   n >  MAX_POSITIONS →  позиций MAX_POSITIONS, размер usable/MAX_POSITIONS (фаза 2)
#
# Примеры: $22 → 3 × $5 | $50 → 7 × $5 | $80 → 12 × $5 | $100 → 12 × $6.25 |
#          $200 → 12 × $12.50 | $500 → 12 × $31.25
#
CAPITAL_USE = 0.75      # 25% держим свободными: запас на просадку, комиссии, фандинг
MIN_POS_USD = 5.0       # минимальный номинал ордера на Bybit
MAX_POSITIONS = 12      # потолок числа позиций: при 8+ лимит уже не режет поток
                        # сигналов (835 из 835 разбираются), 12 — с запасом
MAX_POS = 100.0         # абсолютный потолок размера сделки, страховка от ошибки

# Оставлено для обратной совместимости с внешними скриптами, читающими TIERS.
TIERS = [{'min_balance': 0, 'pos_usdt': MIN_POS_USD, 'name': 'linear'}]
MIN_POS = 1
CONFIG_PATH = Path('/home/andy/CryptoTrader_main/compound_state.json')


def get_sizing(balance: float) -> dict:
    """По свободному балансу вернуть (число позиций, размер сделки).

    Фаза 1 — растёт число позиций при минимальном размере.
    Фаза 2 — число позиций упёрлось в потолок, дальше растёт размер.
    """
    usable = max(0.0, balance) * CAPITAL_USE
    n_by_min = int(usable // MIN_POS_USD)

    if n_by_min <= 0:
        return {'max_positions': 1, 'pos_usdt': MIN_POS_USD, 'phase': 'ниже минимума',
                'usable': usable, 'exposure': MIN_POS_USD}
    if n_by_min <= MAX_POSITIONS:
        positions, pos = n_by_min, MIN_POS_USD
        phase = 'фаза 1: растёт число позиций'
    else:
        positions = MAX_POSITIONS
        pos = min(MAX_POS, int(usable / MAX_POSITIONS * 100) / 100)
        phase = 'фаза 2: растёт размер сделки'
    return {'max_positions': positions, 'pos_usdt': pos, 'phase': phase,
            'usable': usable, 'exposure': positions * pos}


def get_tier(balance: float) -> dict:
    """Обратная совместимость: старые вызовы ждут {'tier': ..., 'pos_usdt': ...}."""
    s = get_sizing(balance)
    return {'tier': {'name': s['phase'], 'min_balance': 0}, 'pos_usdt': s['pos_usdt'],
            'max_positions': s['max_positions']}


def get_bybit_balance():
    """Получить USDT баланс на Bybit (linear).
    
    R2 FIX: возвращает None при ошибке API (не 0.0 — неотличимо от пустого счёта).
    Вызывающий код обязан проверять None перед использованием.
    """
    try:
        from cryptotrader_strategies.bybit_safe import bybit_exchange
        ex = bybit_exchange(with_auth=True)
        bal = ex.fetch_balance({'type': 'swap', 'accountType': 'UNIFIED'})
        usdt_info = bal.get('USDT') or {}
        free = usdt_info.get('free', 0.0) or 0.0
        return float(free)
    except Exception as e:
        print(f"ERROR fetching balance: {e}", flush=True)
        return None  # R2: None = «неизвестно», НЕ 0.0


def load_state() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {
        'current_pos_usdt': 5.0,
        'last_balance': 0.0,
        'last_update': None,
        'tier': 'starter',
        'history': [],
    }


def save_state(state: dict):
    state['last_update'] = datetime.now(timezone.utc).isoformat()
    CONFIG_PATH.write_text(json.dumps(state, indent=2, default=str))


def update_settings_max_positions(n: int) -> tuple:
    """Синхронизировать agents.risk.max_open_positions_total в settings.yaml.

    Нужно, потому что лимит проверяется в двух местах: сканер берёт его из
    compound_state, а execution_agent — из settings.yaml. Если их не свести,
    более строгий молча срежет сигналы. Запись с бэкапом и валидацией: битый
    YAML уронил бы исполнителя целиком.
    """
    import shutil
    import re as _re
    path = Path('/home/andy/CryptoTrader_main/config/settings.yaml')
    try:
        src = path.read_text()
        new, cnt = _re.subn(r'(^\s*max_open_positions_total:\s*)\d+',
                            lambda m: f"{m.group(1)}{n}", src, count=1, flags=_re.M)
        if cnt == 0:
            return False, "ключ max_open_positions_total не найден"
        if new == src:
            return True, "уже актуально"
        backup = path.with_suffix(f'.yaml.bak.compound.{datetime.now():%Y%m%d_%H%M%S}')
        shutil.copy2(path, backup)
        path.write_text(new)
        import yaml as _yaml
        got = _yaml.safe_load(path.read_text())['agents']['risk']['max_open_positions_total']
        if int(got) != int(n):
            shutil.copy2(backup, path)
            return False, f"откат: после записи прочитано {got}"
        return True, f"обновлено до {n} (бэкап: {backup.name})"
    except Exception as e:
        return False, f"ошибка: {e}"


def cmd_status():
    bal = get_bybit_balance()
    if bal is None:
        print("ERROR: balance unavailable (API error), cannot determine sizing", flush=True)
        return {'balance': None, 'pos_usdt': None, 'max_positions': None}
    s = get_sizing(bal)
    state = load_state()
    print(f"=== Compound Status ===", flush=True)
    print(f"Свободный баланс:   ${bal:.2f}", flush=True)
    print(f"В работе ({CAPITAL_USE:.0%}):     ${s['usable']:.2f}", flush=True)
    print(f"Режим:              {s['phase']}", flush=True)
    print(f"Позиций:            {s['max_positions']} (потолок {MAX_POSITIONS})", flush=True)
    print(f"Размер сделки:      ${s['pos_usdt']:.2f}", flush=True)
    print(f"Максимум в рынке:   ${s['exposure']:.2f}", flush=True)
    print(f"Сейчас в state:     {state.get('current_max_positions', '?')} × "
          f"${state.get('current_pos_usdt', 0):.2f}", flush=True)
    print(f"Последний ребаланс: {state.get('last_update', 'never')}", flush=True)
    return {'balance': bal, 'pos_usdt': s['pos_usdt'], 'max_positions': s['max_positions']}


def cmd_simulate(start_balance: float = 22.5, exp_pct: float = 0.144):
    """Траектория роста при новой схеме масштабирования.

    Поток сделок зависит от числа позиций — по бэктесту v9 CTL на 27 парах:
    3 позиции ≈ 5.8 сделок/день, 12 позиций ≈ 10.8. Между ними линейно.

    exp_pct по умолчанию 0.144%/сделку — ЧЕСТНАЯ оценка с holdout, а не
    in-sample 0.29%. Прогноз без учёта просадок: реальная кривая будет
    заметно менее гладкой.
    """
    print(f"=== Прогноз роста (v9 CTL, {exp_pct:.3f}%/сделку) ===", flush=True)
    print(f"Старт ${start_balance:.2f}, схема: сначала число позиций, потом размер\n", flush=True)
    print(f"{'день':<6}{'баланс':>10}{'позиций':>9}{'размер':>9}{'сделок/д':>10}{'рост':>9}", flush=True)
    print("-" * 55, flush=True)
    bal = start_balance
    for day in range(1, 366):
        s = get_sizing(bal)
        n, pos = s['max_positions'], s['pos_usdt']
        trades_day = 4.13 + 0.556 * n          # аппроксимация из бэктеста
        bal += trades_day * pos * exp_pct / 100
        if day in (1, 7, 30, 60, 90, 180, 270, 365):
            print(f"{day:<6}{bal:>9.2f}${n:>9}{pos:>8.2f}${trades_day:>10.1f}"
                  f"{(bal/start_balance-1)*100:>8.1f}%", flush=True)
    print("-" * 55, flush=True)
    print(f"ИТОГ за год: ${start_balance:.2f} → ${bal:.2f} ({(bal/start_balance-1)*100:+.1f}%)", flush=True)
    print("\nПрогноз опирается на неподтверждённый эдж — принимать как ориентир,", flush=True)
    print("а не как план. Реальную оценку даст живая статистика за несколько сотен сделок.", flush=True)
    return bal


def cmd_rebalance(force=False):
    bal = get_bybit_balance()
    # R2 FIX: не ребалансить при ошибке API (None) или невалидном балансе
    if bal is None or bal <= 0.0:
        print(f"Balance unavailable/zero ({bal}) — skip rebalance (no state mutation)", flush=True)
        return
    s = get_sizing(bal)
    state = load_state()
    cur_pos = float(state.get('current_pos_usdt', MIN_POS_USD))
    cur_n = int(state.get('current_max_positions', 0) or 0)
    delta_pos = abs(s['pos_usdt'] - cur_pos)
    delta_n = s['max_positions'] != cur_n

    if delta_pos < 0.5 and not delta_n and not force:
        print(f"Без изменений: {cur_n} × ${cur_pos:.2f} (delta ${delta_pos:.2f}) — "
              f"пропуск (--force чтобы применить)", flush=True)
        return

    state['current_pos_usdt'] = s['pos_usdt']
    state['current_max_positions'] = s['max_positions']
    state['last_balance'] = bal
    state['tier'] = s['phase']
    state.setdefault('history', []).append({
        'ts': datetime.now(timezone.utc).isoformat(),
        'balance': bal,
        'pos_usdt': s['pos_usdt'],
        'max_positions': s['max_positions'],
        'tier': s['phase'],
    })
    save_state(state)
    ok, msg = update_settings_max_positions(s['max_positions'])
    print(f"✓ Пересчитано: ${bal:.2f} свободных → {cur_n} × ${cur_pos:.2f} "
          f"стало {s['max_positions']} × ${s['pos_usdt']:.2f} "
          f"(в рынке максимум ${s['exposure']:.2f}, {s['phase']})", flush=True)
    print(f"  settings.yaml: {'✓' if ok else '✗'} {msg}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--simulate', action='store_true')
    parser.add_argument('--rebalance', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.simulate:
        cmd_simulate()
    elif args.rebalance:
        cmd_rebalance(force=args.force)
    else:
        cmd_status()
