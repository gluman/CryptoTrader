#!/usr/bin/env python3
"""
OOS Live Validator — сравнивает живые результаты v9 CTL с ожиданиями бэктеста.
Ожидания честные (holdout + посторонние пары): WR 58.2%, PF 1.38, +0.144%/сделку.

Использование:
    python oos_live_validator.py          # одноразовая проверка
    python oos_live_validator.py --alert   # отправить алерт если drift > 30%
"""
import os, sys, json, argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Единый timezone helper (MSK UTC+3) для отчётов Boss
sys.path.insert(0, '/home/andy/.hermes/scripts')
from time_utils import now_msk_str, msk_iso_now  # noqa: E402

sys.path.insert(0, '/home/andy/CryptoTrader_main')
from dotenv import load_dotenv
load_dotenv('/home/andy/CryptoTrader_main/.env')

import psycopg2

# R4+R13 FIX: os.environ.get + единый источник DSN через db_safe.db_dsn()
from cryptotrader_strategies.db_safe import db_dsn
DB = db_dsn()


def get_live_trades(days=14):
    """Получить закрытые позиции v9 CTL за последние N дней.

    [06.08.2026] Было notes LIKE '%clone5_v7%' и status='closed' строчными —
    v7 не торгует с июля, а в БД статус пишется 'CLOSED'. Валидатор физически
    не мог ничего найти и год отвечал бы "нет сделок".
    """
    conn = psycopg2.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT symbol, side, realized_pnl, notes, opened_at, closed_at
            FROM positions
            WHERE upper(exchange)='BYBIT'
              AND upper(status)='CLOSED'
              AND notes LIKE '%clone5_v9%'
              AND closed_at > NOW() - INTERVAL '{days} days'
            ORDER BY closed_at DESC
        """)
        return cur.fetchall()
    finally:
        conn.close()


def compute_live_metrics(trades):
    if not trades:
        return None
    pnls = [t[2] or 0.0 for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / len(pnls) * 100
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p <= 0))
    pf = gross_w / gross_l if gross_l > 0 else (10.0 if gross_w > 0 else 0.0)
    return {
        'trades': len(pnls),
        'wr_pct': wr,
        'pf': pf,
        'pnl_usd': sum(pnls),
        'wins': wins,
        'losses': len(pnls) - wins,
    }


# === Ожидания v9 CTL ===
# Взяты ЧЕСТНЫЕ, с holdout и посторонних пар (+0.144%/сделку), а не in-sample
# (+0.29%). Поток: ~5.8 сделок/день при 3 позициях (бэктест 27 пар).
OOS_EXPECTED = {
    'wr_pct': 58.2,
    'pf': 1.38,
    'trades_per_4w': 162,   # 5.8/день × 28
    'pnl_per_4w': 1.17,     # 162 сделки × $5 × 0.144%
}
DRIFT_THRESHOLD = 0.30   # 30% отклонения = дрифт
DRIFT_MIN_TRADES = 20    # ниже этого выборка не показательна


def check_drift(live, expected, period_days=14):
    """Сравнить live с OOS, вернуть список дрифтов."""
    drifts = []
    # [06.08.2026] realized_pnl приходит из БД как Decimal, и метрики наследуют тип.
    # Раньше до этой строки дело не доходило (валидатор не находил сделок), поэтому
    # падение всплыло только сейчас: Decimal - float даёт TypeError.
    live = {k: (float(v) if isinstance(v, (int, float)) or hasattr(v, '__float__') else v)
            for k, v in live.items()}
    scale = period_days / 28.0  # scale to 4-week equivalent
    # WR
    wr_diff = abs(live['wr_pct'] - expected['wr_pct']) / expected['wr_pct']
    if wr_diff > DRIFT_THRESHOLD:
        drifts.append(f"WR drift: live={live['wr_pct']:.1f}% vs OOS={expected['wr_pct']:.1f}% (Δ{wr_diff*100:.0f}%)")
    # PF
    pf_diff = abs(live['pf'] - expected['pf']) / expected['pf']
    if pf_diff > DRIFT_THRESHOLD:
        drifts.append(f"PF drift: live={live['pf']:.2f} vs OOS={expected['pf']:.2f} (Δ{pf_diff*100:.0f}%)")
    # PnL scaled
    expected_pnl_scaled = expected['pnl_per_4w'] * scale
    pnl_diff = abs(live['pnl_usd'] - expected_pnl_scaled)
    if expected_pnl_scaled > 0 and pnl_diff / expected_pnl_scaled > DRIFT_THRESHOLD:
        drifts.append(f"PnL drift: live=${live['pnl_usd']:+.2f} vs expected ${expected_pnl_scaled:+.2f} (Δ${pnl_diff:+.2f})")
    return drifts


# === Авто-алерт при плохих показателях (R6 fix 2026-06-08) ===
# [Boss 2026-06-08 16:15: «OOS Validator 23:00 daily — ок, но результаты не смотрим.
# Если validator падает 3+ дней подряд, никто не замечает. Добавь alert при
# oop_pf < 1.0 или trades < 3 за неделю».]
# Здесь формируем структурированный alert для cron (no_agent=True доставляет
# stdout в Telegram если deliver=telegram:519881679). Сам скрипт остаётся
# молчаливым при зелёном, говорит только когда есть проблема.

ALERT_PF_FLOOR = 1.0       # PF ниже — убыточно
ALERT_TRADES_MIN = 3        # меньше 3 сделок за период — стат. незначимо


def emit_alert(metrics, drifts, days):
    """Финальный вывод. Cron доставит stdout в Telegram.
    Структура: всегда одна JSON-строка в конце + free-form text выше.
    Если всё ок — без JSON (silent), чтобы не спамить.
    """
    if metrics is None:
        # Нет сделок — норма, пока статистика не накопилась
        return
    alerts = []
    if metrics['pf'] < ALERT_PF_FLOOR:
        alerts.append(f"PF {metrics['pf']:.2f} < {ALERT_PF_FLOOR} (убыточно)")
    if metrics['trades'] < ALERT_TRADES_MIN:
        alerts.append(f"Trades {metrics['trades']} < {ALERT_TRADES_MIN} (стат. незначимо)")
    # [06.08.2026] Дрифт на горстке сделок — шум, а не сигнал. Раньше алерт
    # срабатывал уже на 4 сделках, обесценивая уведомления.
    if drifts and metrics['trades'] >= DRIFT_MIN_TRADES:
        alerts.append(f"{len(drifts)} drift(s) > {DRIFT_THRESHOLD*100:.0f}%")
    if alerts:
        # Формируем ALERT-сообщение для Telegram
        print(f"\n🚨 OOS ALERT ({days}d, {now_msk_str()}):", flush=True)
        for a in alerts:
            print(f"   • {a}", flush=True)
        # Машино-читаемый JSON для парсинга (последняя строка stdout)
        import json as _json
        print(_json.dumps({
            "alert": True,
            "pf": metrics['pf'],
            "trades": metrics['trades'],
            "wr_pct": metrics['wr_pct'],
            "pnl_usd": metrics['pnl_usd'],
            "reasons": alerts,
            "days": days,
        }), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--alert', action='store_true')
    parser.add_argument('--days', type=int, default=14)
    args = parser.parse_args()

    print(f"📉 **OOS-валидатор v9** `{args.days}д, {now_msk_str()}`", flush=True)
    print(f"ожидание: WR {OOS_EXPECTED['wr_pct']}%, PF {OOS_EXPECTED['pf']}, "
          f"PnL ${OOS_EXPECTED['pnl_per_4w']*args.days/28:+.2f}", flush=True)

    trades = get_live_trades(args.days)
    if not trades:
        print(f"⏳ нет закрытых сделок v9 за {args.days} дн — ждём накопления статистики", flush=True)
        return None, []

    metrics = compute_live_metrics(trades)
    print(f"Live trades:   {metrics['trades']}", flush=True)
    print(f"Live WR:       {metrics['wr_pct']:.1f}% (OOS: {OOS_EXPECTED['wr_pct']}%)", flush=True)
    print(f"Live PF:       {metrics['pf']:.2f} (OOS: {OOS_EXPECTED['pf']})", flush=True)
    print(f"Live PnL:      ${metrics['pnl_usd']:+.2f}", flush=True)
    print("-"*70, flush=True)

    drifts = check_drift(metrics, OOS_EXPECTED, args.days)
    if drifts:
        note = "" if metrics['trades'] >= DRIFT_MIN_TRADES else \
            f"  (выборка {metrics['trades']} сделок — мало для выводов)"
        print(f"⚠️  отклонения от ожиданий:{note}", flush=True)
        for d in drifts:
            print(f"  • {d}", flush=True)
        if args.alert:
            print(f"\n🚨 отклонение от ожиданий v9 — проверить параметры", flush=True)
    else:
        print(f"✅ NO DRIFT — live performance matches OOS expectations", flush=True)
        print(f"   Continue compound", flush=True)

    # [06.08.2026] Группировка шла по полному тексту notes, где у каждой сделки
    # свой orderId — получался список из N строк по одной сделке в каждой.
    # Показываем разбивку по парам: по ней видно, кто тянет результат.
    by_sym = {}
    for t in trades:
        sym = t[0]
        d = by_sym.setdefault(sym, {'n': 0, 'pnl': 0.0, 'w': 0})
        d['n'] += 1
        d['pnl'] += float(t[2] or 0)
        d['w'] += 1 if float(t[2] or 0) > 0 else 0
    print("по парам:", flush=True)
    for sym, d in sorted(by_sym.items(), key=lambda x: x[1]['pnl']):
        print(f"  {sym:<11} n={d['n']:>3}  WR={100*d['w']/d['n']:>5.1f}%  {d['pnl']:+.3f}$", flush=True)

    # Авто-алерт (R6 fix)
    emit_alert(metrics, drifts, args.days)

    return metrics, drifts


if __name__ == "__main__":
    main()
