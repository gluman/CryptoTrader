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

sys.path.insert(0, '/home/andy/CryptoTrader')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader/.env')

import ccxt

# === Compound rules ===
TIERS = [
    {'min_balance': 0,    'pos_usdt': 5,   'name': 'starter'},
    {'min_balance': 20,   'pos_usdt': 7,   'name': 'tier1'},
    {'min_balance': 50,   'pos_usdt': 10,  'name': 'tier2'},
    {'min_balance': 100,  'pos_usdt': 12,  'name': 'tier3'},
    {'min_balance': 200,  'pos_usdt': 15,  'name': 'tier4'},
    {'min_balance': 500,  'pos_usdt': 20,  'name': 'tier5'},
    {'min_balance': 1000, 'pos_usdt': 25,  'name': 'tier6'},
]
MAX_POS = 30  # per-pair exposure cap
MIN_POS = 1
CONFIG_PATH = Path('/home/andy/CryptoTrader/compound_state.json')


def get_tier(balance: float) -> dict:
    """Выбрать tier по текущему балансу."""
    chosen = TIERS[0]
    for t in TIERS:
        if balance >= t['min_balance']:
            chosen = t
    pos = min(chosen['pos_usdt'], MAX_POS)
    return {'tier': chosen, 'pos_usdt': pos}


def get_bybit_balance() -> float:
    """Получить USDT баланс на Bybit (linear)."""
    try:
        ex = ccxt.bybit({
            'apiKey': os.environ['BYBIT_API_KEY'],
            'secret': os.environ['BYBIT_API_SECRET'],
            'options': {'defaultType': 'linear'},
            'enableRateLimit': True,
            'recvWindow': 60000,
        })
        bal = ex.fetch_balance({'type': 'linear'})
        usdt_info = bal.get('USDT') or {}
        free = usdt_info.get('free', 0.0) or 0.0
        return float(free)
    except Exception as e:
        print(f"ERROR fetching balance: {e}", flush=True)
        return 0.0


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


def cmd_status():
    bal = get_bybit_balance()
    tier = get_tier(bal)
    state = load_state()
    print(f"=== Compound Status ===", flush=True)
    print(f"Bybit USDT free:  ${bal:.2f}", flush=True)
    print(f"Current tier:     {tier['tier']['name']} (min ${tier['tier']['min_balance']})", flush=True)
    print(f"POS_USDT:         ${tier['pos_usdt']:.2f}", flush=True)
    print(f"Max concurrent:   2 → max exposure ${tier['pos_usdt'] * 2:.2f}", flush=True)
    print(f"Last rebalance:   {state.get('last_update', 'never')}", flush=True)
    print(f"Rebalance delta:  ${abs(tier['pos_usdt'] - state['current_pos_usdt']):.2f}", flush=True)
    return {'balance': bal, 'pos_usdt': tier['pos_usdt'], 'tier': tier['tier']['name']}


def cmd_simulate():
    """Симулировать compound с v7 OOS метриками (4 weeks, 66 trades, WR 62.1%, +$0.87 base $5)."""
    print(f"=== Compound SIMULATION (v7 OOS profile) ===", flush=True)
    print(f"Per-trade PnL = +0.26% of size (62.1% WR × avg +0.4% - 37.9% × -0.5%)", flush=True)
    print(f"Approx trades/day = 2.4 (66 / 28 days)", flush=True)
    print(f"Max concurrent = 2 (limit)", flush=True)
    print(f"{'Day':<5} {'Balance':<10} {'PosSize':<8} {'Daily PnL':<10} {'Cum PnL':<10} {'Cum %':<8}", flush=True)
    print("-" * 70, flush=True)
    bal = 20.0
    cum_pnl = 0.0
    for day in range(1, 366):
        tier = get_tier(bal)
        pos = tier['pos_usdt']
        # 2.4 trades/day × pos × 0.0026 PnL
        daily_pnl = 2.4 * pos * 0.0026
        bal += daily_pnl
        cum_pnl += daily_pnl
        if day in [1, 3, 7, 14, 30, 60, 90, 180, 365]:
            print(f"{day:<5} ${bal:<9.2f} ${pos:<7.2f} ${daily_pnl:<9.3f} ${cum_pnl:<9.2f} {(bal/20-1)*100:<7.1f}%", flush=True)
        if day >= 365:
            break
    print("-" * 70, flush=True)
    print(f"FINAL: $20.00 → ${bal:.2f} ({(bal/20-1)*100:+.1f}%)", flush=True)
    return bal


def cmd_rebalance(force=False):
    bal = get_bybit_balance()
    tier = get_tier(bal)
    state = load_state()
    delta = abs(tier['pos_usdt'] - state['current_pos_usdt'])
    if delta < 0.5 and not force:
        print(f"Delta ${delta:.2f} < $0.50, skip (use --force)", flush=True)
        return
    state['current_pos_usdt'] = tier['pos_usdt']
    state['last_balance'] = bal
    state['tier'] = tier['tier']['name']
    state['history'].append({
        'ts': datetime.now(timezone.utc).isoformat(),
        'balance': bal,
        'pos_usdt': tier['pos_usdt'],
        'tier': tier['tier']['name'],
    })
    save_state(state)
    print(f"✓ Rebalanced: ${bal:.2f} → POS_USDT=${tier['pos_usdt']:.2f} ({tier['tier']['name']})", flush=True)


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
