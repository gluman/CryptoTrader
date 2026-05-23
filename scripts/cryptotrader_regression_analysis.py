#!/usr/bin/env python3
"""
CryptoTrader Regression Analysis
Queries last 14d closed positions + signals from DB,
analyzes entry/exit quality, classifies factors,
outputs plain text report to stdout.
"""
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Load .env manually
env = {}
for line in Path('/home/andy/.env').read_text().splitlines():
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        env[k] = v
        os.environ[k] = v

import psycopg2

# ── DB Connection ──
def get_conn():
    return psycopg2.connect(
        host=env.get('POSTGRES_HOST', '127.0.0.1'),
        port=int(env.get('POSTGRES_PORT', '5432')),
        dbname=env.get('POSTGRES_DB', 'cryptotrader'),
        user=env.get('POSTGRES_USER', 'cryptotrader'),
        password=env.get('POSTGRES_PASSWORD', ''),
    )

# ── Factor Classifier ──
def classify_factor(symbol: str, side: str, entry: float, close: float,
                    sl: float, tp: float, signal_type: str, strength: float) -> str:
    """Classify the primary factor behind the trade outcome."""
    pnl_pct = ((close - entry) / entry) * 100 if entry else 0
    if side == 'short':
        pnl_pct = -pnl_pct

    # TP/SL hit
    if tp and close > 0:
        if side == 'long' and tp > 0 and close >= tp * 0.998:
            return "TP_hit"
        if side == 'short' and tp > 0 and close <= tp * 1.002:
            return "TP_hit"
        if sl and close > 0:
            if side == 'long' and sl > 0 and close <= sl * 1.002:
                return "SL_hit"
            if side == 'short' and sl > 0 and close >= sl * 0.998:
                return "SL_hit"

    # Signal quality
    if signal_type == 'buy' and side == 'long':
        return "long_signal"
    if signal_type == 'sell' and side == 'short':
        return "short_signal"
    if signal_type == 'buy' and side == 'short':
        return "reverse_signal"
    if signal_type == 'sell' and side == 'long':
        return "reverse_signal"

    # High confidence
    if strength and strength >= 0.8:
        return "high_confidence"

    # Small pnl
    if abs(pnl_pct) < 0.5:
        return "low_volatility"

    return "other"


# ── Execution Quality ──
def exec_quality(entry_price: float, signal_price: float, side: str) -> str:
    """Assess how well execution matched signal price."""
    if not signal_price or not entry_price or signal_price == 0:
        return "N/A"
    slippage_pct = abs(entry_price - signal_price) / signal_price * 100
    if slippage_pct < 0.1:
        return f"excellent ({slippage_pct:.3f}%)"
    elif slippage_pct < 0.5:
        return f"good ({slippage_pct:.3f}%)"
    elif slippage_pct < 1.0:
        return f"fair ({slippage_pct:.3f}%)"
    else:
        return f"poor ({slippage_pct:.3f}%)"


def run() -> str:
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=14)

    lines = []
    lines.append("=" * 60)
    lines.append("CRYPTO TRADER – REGRESSION ANALYSIS REPORT")
    lines.append(f"Period: last 14 days ({since.date()} to {now.date()})")
    lines.append(f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 60)

    # ── 1. Closed Positions ──
    cur.execute("""
        SELECT id, symbol, exchange, side, entry_price, close_price,
               quantity, cost_usdt, stop_loss, take_profit,
               realized_pnl, realized_pnl_percent,
               opened_at, closed_at, status,
               EXTRACT(EPOCH FROM (closed_at - opened_at)) as duration_sec
        FROM positions
        WHERE closed_at IS NOT NULL
          AND closed_at >= %s
        ORDER BY closed_at DESC
    """, (since,))
    positions = cur.fetchall()

    if not positions:
        lines.append("\n📊 CLOSED POSITIONS (14d)")
        lines.append("  No closed positions in the last 14 days.")
    else:
        lines.append(f"\n📊 CLOSED POSITIONS (14d) — {len(positions)} trades")
        total_pnl = 0.0
        winners = 0
        losers = 0
        factors = {}

        for p in positions:
            pnl = float(p[10]) if p[10] else 0.0
            pnl_pct = float(p[11]) if p[11] else 0.0
            dur_h = float(p[15]) / 3600 if p[15] else 0
            side = p[3]
            entry = float(p[4]) if p[4] else 0
            close = float(p[5]) if p[5] else 0
            sl = float(p[8]) if p[8] else None
            tp = float(p[9]) if p[9] else None
            total_pnl += pnl

            if pnl > 0:
                winners += 1
            elif pnl < 0:
                losers += 1

            factor = classify_factor(p[1], side, entry, close, sl, tp, None, None)
            factors[factor] = factors.get(factor, 0) + 1

            direction = "📈" if side == 'long' else "📉"
            lines.append(f"  {direction} {p[1]} | {side.upper()} | qty={p[6]} | entry={entry:.4f} close={close:.4f}")
            lines.append(f"     PnL: {pnl:+.2f} USDT ({pnl_pct:+.2f}%) | dur={dur_h:.1f}h | {p[0]}")
            if sl:
                lines.append(f"     SL={sl:.4f} TP={tp:.4f if tp else 'N/A'}")
            lines.append(f"     Factor: {factor}")

        lines.append("-" * 40)
        lines.append(f"  Total PnL: {total_pnl:+.2f} USDT")
        lines.append(f"  Winners: {winners} | Losers: {losers} | Win rate: {winners/(winners+losers)*100:.1f}%")
        lines.append(f"  Factor breakdown: {factors}")

    # ── 2. Signals (14d) ──
    cur.execute("""
        SELECT id, symbol, signal_type, strength, confidence,
               status, executed_at, pnl_percent, pnl_absolute,
               price, css_value, rsi_14, macd, atr_14,
               sentiment_score, news_volume, volume_24h,
               created_at
        FROM signals
        WHERE created_at >= %s
        ORDER BY created_at DESC
    """, (since,))
    signals = cur.fetchall()

    if not signals:
        lines.append("\n📡 SIGNALS (14d)")
        lines.append("  No signals generated in the last 14 days.")
    else:
        pending = [s for s in signals if s[5] == 'pending']
        executed = [s for s in signals if s[5] == 'executed']
        expired = [s for s in signals if s[5] == 'expired']

        lines.append(f"\n📡 SIGNALS (14d) — {len(signals)} total")
        lines.append(f"  Executed: {len(executed)} | Pending: {len(pending)} | Expired: {len(expired)}")

        # Confidence distribution
        conf_buckets = {'>=0.8': 0, '0.5-0.8': 0, '<0.5': 0}
        for s in signals:
            conf = float(s[4]) if s[4] else 0
            if conf >= 0.8:
                conf_buckets['>=0.8'] += 1
            elif conf >= 0.5:
                conf_buckets['0.5-0.8'] += 1
            else:
                conf_buckets['<0.5'] += 1
        lines.append(f"  Confidence dist: {conf_buckets}")

        # Executed signal details
        if executed:
            lines.append("\n  Top executed signals:")
            for s in executed[:10]:
                pnl_pct = float(s[7]) if s[7] else 0
                pnl_abs = float(s[8]) if s[8] else 0
                lines.append(f"    {s[1]} {s[2]} conf={float(s[4]):.2f} css={float(s[10]):.2f} rsi={float(s[11]):.1f} pnl={pnl_pct:+.2f}%")

        # Factor: model version quality
        if executed:
            lines.append("\n  Execution by signal type:")
            by_type = {}
            for s in executed:
                by_type[s[2]] = by_type.get(s[2], []) + [float(s[7]) if s[7] else 0]
            for st, pnls in by_type.items():
                avg = sum(pnls) / len(pnls) if pnls else 0
                lines.append(f"    {st}: count={len(pnls)} avg_pnl={avg:+.2f}%")

    # ── 3. Entry/Exit Quality ──
    if positions:
        lines.append("\n🎯 ENTRY/EXIT QUALITY ANALYSIS")
        sl_hits = sum(1 for p in positions
                      if p[8] and float(p[5]) and p[3] == 'long' and float(p[5]) <= float(p[8]) * 1.002)
        sl_hits += sum(1 for p in positions
                       if p[8] and float(p[5]) and p[3] == 'short' and float(p[5]) >= float(p[8]) * 0.998)
        tp_hits = sum(1 for p in positions
                      if p[9] and float(p[5]) and p[3] == 'long' and float(p[5]) >= float(p[9]) * 0.998)
        tp_hits += sum(1 for p in positions
                       if p[9] and float(p[5]) and p[3] == 'short' and float(p[5]) <= float(p[9]) * 1.002)

        total = len(positions)
        lines.append(f"  SL hits: {sl_hits} ({sl_hits/total*100:.1f}%)")
        lines.append(f"  TP hits: {tp_hits} ({tp_hits/total*100:.1f}%)")
        lines.append(f"  Other exit: {total - sl_hits - tp_hits} ({(total-sl_hits-tp_hits)/total*100:.1f}%)")

        # Average duration
        durations = [float(p[15]) for p in positions if p[15]]
        if durations:
            avg_dur_h = sum(durations) / len(durations) / 3600
            lines.append(f"  Avg trade duration: {avg_dur_h:.2f}h")

    # ── 4. Strategy Signals (14d) ──
    cur.execute("""
        SELECT id, symbol, strategy, action, confidence,
               entry_price, stop_loss, take_profit,
               status, pnl_percent, pnl_absolute,
               created_at, executed_at
        FROM strategy_signals
        WHERE created_at >= %s
        ORDER BY created_at DESC
    """, (since,))
    strat_signals = cur.fetchall()

    if strat_signals:
        lines.append(f"\n🧠 STRATEGY SIGNALS (14d) — {len(strat_signals)} total")
        by_strat = {}
        for ss in strat_signals:
            strat = ss[2] or 'unknown'
            if strat not in by_strat:
                by_strat[strat] = {'count': 0, 'pnl': [], 'conf': []}
            by_strat[strat]['count'] += 1
            if ss[9]:
                by_strat[strat]['pnl'].append(float(ss[9]))
            if ss[4]:
                by_strat[strat]['conf'].append(float(ss[4]))

        for strat, data in by_strat.items():
            avg_pnl = sum(data['pnl']) / len(data['pnl']) if data['pnl'] else 0
            avg_conf = sum(data['conf']) / len(data['conf']) if data['conf'] else 0
            lines.append(f"  {strat}: count={data['count']} avg_pnl={avg_pnl:+.2f}% avg_conf={avg_conf:.2f}")
    else:
        lines.append("\n🧠 STRATEGY SIGNALS (14d) — none in period")

    # ── 5. Recent decisions ──
    cur.execute("""
        SELECT id, timestamp, llm_model, latency_ms, prompt_tokens,
               completion_tokens, total_tokens, decision_json, created_at
        FROM decisions
        WHERE created_at >= %s
        ORDER BY created_at DESC
        LIMIT 5
    """, (since,))
    decisions = cur.fetchall()
    if decisions:
        lines.append(f"\n🤖 RECENT DECISIONS ({len(decisions)} shown)")
        for d in decisions:
            decision = d[7] or {}
            action = decision.get('action', 'N/A') if isinstance(decision, dict) else 'N/A'
            symbol = decision.get('symbol', 'N/A') if isinstance(decision, dict) else 'N/A'
            conf = decision.get('confidence', 0) if isinstance(decision, dict) else 0
            lines.append(f"  {d[8]} | {symbol} {action} | model={d[2][:30]} lat={d[3]}ms")
    else:
        lines.append("\n🤖 DECISIONS (14d) — none in period")

    cur.close()
    conn.close()

    lines.append("\n" + "=" * 60)
    lines.append("END OF REPORT")
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == '__main__':
    print(run())