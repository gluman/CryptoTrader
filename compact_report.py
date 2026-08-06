#!/usr/bin/env python3
"""
compact_report.py — Компактный отчёт по позициям для Telegram
Формат: SYMBOL PNL$ SIDE [✓]

СТАТУС (06.08.2026): ни один cron этот скрипт не вызывает — позиции и PnL
публикует scan_and_execute_tg.sh. Оставлен как готовый инструмент для ручного
запуска; если не понадобится — удалить.

[Fix 06.08.2026] side сравнивался с 'long' строчными, в БД — 'LONG': все
длинные позиции отображались как 'S' (шорт).
"""
import sys
sys.path.insert(0, '/home/andy/CryptoTrader_main')

import psycopg2
from datetime import datetime

def get_positions():
    """Получить открытые и закрытые позиции за 24ч"""
    conn = psycopg2.connect(
        host='127.0.0.1',
        port=5432,
        dbname='cryptotrader',
        user='cryptotrader',
        password='cryptotrader123'
    )
    cur = conn.cursor()
    
    # Открытые позиции
    cur.execute("""
        SELECT symbol, side, entry_price, unrealized_pnl
        FROM positions
        WHERE status = 'OPEN'
        ORDER BY symbol
    """)
    open_pos = cur.fetchall()
    
    # Закрытые за 24ч
    cur.execute("""
        SELECT symbol, side, realized_pnl
        FROM positions
        WHERE status = 'CLOSED'
        AND closed_at > NOW() - INTERVAL '24 hours'
        ORDER BY closed_at DESC
    """)
    closed_pos = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return open_pos, closed_pos

def format_report():
    """Форматировать отчёт"""
    open_pos, closed_pos = get_positions()
    
    lines = []
    
    # Открытые позиции
    for sym, side, entry, upnl in open_pos:
        side_short = 'B' if (side or '').upper() == 'LONG' else 'S'
        upnl_str = f"+{upnl:.2f}" if upnl >= 0 else f"{upnl:.2f}"
        lines.append(f"{sym:<10} {upnl_str:>7}$ {side_short}")
    
    # Закрытые (последние 5)
    for sym, side, rpnl in closed_pos[:5]:
        side_short = 'B' if (side or '').upper() == 'LONG' else 'S'
        rpnl_str = f"+{rpnl:.2f}" if rpnl >= 0 else f"{rpnl:.2f}"
        lines.append(f"{sym:<10} {rpnl_str:>7}$ {side_short} ✓")
    
    return '\n'.join(lines) if lines else "Нет позиций"

if __name__ == '__main__':
    print(format_report())
