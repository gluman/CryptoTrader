"""
Уведомления для Telegram — читаемый формат на русском
"""

import sys
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
import psycopg2

# Add project root to path for BybitAPI
sys.path.insert(0, '/home/andy')


class TelegramFormatter:
    """Форматирование данных для Telegram на русском языке"""

    def __init__(self, db_config: dict):
        self.db_config = db_config

    def _get_connection(self):
        return psycopg2.connect(
            host=self.db_config.get('host', '127.0.0.1'),
            port=self.db_config.get('port', 5433),
            dbname=self.db_config.get('database', 'cryptotrader'),
            user=self.db_config.get('user', 'andy'),
            password=self.db_config.get('password', 'Glumov555')
        )

    def _get_bybit_price(self, symbol: str) -> float:
        """Получить актуальную цену с Bybit (всегда свежая)"""
        try:
            from src.core.config import Config
            from src.gateways.bybit_api import BybitAPI
            config = Config()
            api = BybitAPI(
                api_key=config.bybit['api_key'],
                api_secret=config.bybit['api_secret'],
                testnet=config.bybit.get('testnet', False)
            )
            ticker = api.get_ticker(symbol)
            price = float(ticker.get('lastPrice', 0))
            return price if price > 0 else 0.0
        except Exception as e:
            return 0.0

    def get_open_positions(self) -> List[Dict]:
        """Получить открытые позиции"""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, symbol, side, entry_price, quantity, cost_usdt,
                   stop_loss, take_profit, opened_at, unrealized_pnl, unrealized_pnl_percent
            FROM positions
            WHERE UPPER(status) = 'OPEN'
            ORDER BY opened_at DESC
        """)
        rows = cur.fetchall()
        conn.close()

        return [{
            'id': r[0],
            'symbol': r[1],
            'side': r[2],
            'entry_price': float(r[3]),
            'quantity': float(r[4]),
            'cost_usdt': float(r[5]),
            'stop_loss': float(r[6]) if r[6] else None,
            'take_profit': float(r[7]) if r[7] else None,
            'opened_at': r[8],
            'unrealized_pnl': float(r[9]) if r[9] else 0,
            'unrealized_pnl_percent': float(r[10]) if r[10] else 0,
        } for r in rows]

    def get_recent_signals(self, limit: int = 10) -> List[Dict]:
        """Получить последние сигналы"""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT id, symbol, strategy, action, confidence, entry_price,
                   stop_loss, take_profit, reasoning, status, created_at
            FROM strategy_signals
            ORDER BY created_at DESC
            LIMIT {limit}
        """)
        rows = cur.fetchall()
        conn.close()

        return [{
            'id': r[0],
            'symbol': r[1],
            'strategy': r[2],
            'action': r[3],
            'confidence': float(r[4]),
            'entry_price': float(r[5]) if r[5] else None,
            'stop_loss': float(r[6]) if r[6] else None,
            'take_profit': float(r[7]) if r[7] else None,
            'reasoning': r[8],
            'status': r[9],
            'created_at': r[10],
        } for r in rows]

    def get_closed_positions_24h(self) -> List[Dict]:
        """Получить закрытые позиции за 24 часа"""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, symbol, side, entry_price, close_price, quantity,
                   cost_usdt, realized_pnl, realized_pnl_percent,
                   opened_at, closed_at
            FROM positions
            WHERE UPPER(status) = 'CLOSED' AND closed_at > NOW() - INTERVAL '24 hours'
            ORDER BY closed_at DESC
        """)
        rows = cur.fetchall()
        conn.close()

        return [{
            'id': r[0],
            'symbol': r[1],
            'side': r[2],
            'entry_price': float(r[3]),
            'close_price': float(r[4]) if r[4] else None,
            'quantity': float(r[5]),
            'cost_usdt': float(r[6]),
            'realized_pnl': float(r[7]) if r[7] else 0,
            'realized_pnl_percent': float(r[8]) if r[8] else 0,
            'opened_at': r[9],
            'closed_at': r[10],
        } for r in rows]

    def get_closed_positions_all(self, limit: int = 10) -> List[Dict]:
        """Получить все закрытые позиции"""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT id, symbol, side, entry_price, close_price, quantity,
                   cost_usdt, realized_pnl, realized_pnl_percent,
                   opened_at, closed_at
            FROM positions
            WHERE UPPER(status) = 'CLOSED'
            ORDER BY closed_at DESC
            LIMIT {limit}
        """)
        rows = cur.fetchall()
        conn.close()

        return [{
            'id': r[0],
            'symbol': r[1],
            'side': r[2],
            'entry_price': float(r[3]),
            'close_price': float(r[4]) if r[4] else None,
            'quantity': float(r[5]),
            'cost_usdt': float(r[6]),
            'realized_pnl': float(r[7]) if r[7] else 0,
            'realized_pnl_percent': float(r[8]) if r[8] else 0,
            'opened_at': r[9],
            'closed_at': r[10],
        } for r in rows]

    def format_signal(self, s: Dict) -> str:
        """Форматировать один сигнал"""
        action_emoji = '🟢' if s['action'] == 'BUY' else '🔴' if s['action'] == 'SELL' else '⚪'
        action_text = 'ПОКУПКА' if s['action'] == 'BUY' else 'ПРОДАЖА' if s['action'] == 'SELL' else s['action']

        status_emoji = '⏳' if s['status'] == 'pending' else '✅' if s['status'] == 'executed' else '❌' if s['status'] == 'cancelled' else '❓'

        lines = [
            f"{action_emoji} {s['symbol']} — {action_text}",
            f"   Стратегия: {s['strategy']}",
            f"   Уверенность: {s['confidence']*100:.0f}% {status_emoji}",
        ]

        if s.get('entry_price'):
            lines.append(f"   Вход: {s['entry_price']:.2f}")
        if s.get('stop_loss'):
            lines.append(f"   SL: {s['stop_loss']:.2f}")
        if s.get('take_profit'):
            lines.append(f"   TP: {s['take_profit']:.2f}")
        if s.get('reasoning'):
            lines.append(f"   ({s['reasoning'][:80]})")

        return '\n'.join(lines)

    def format_position(self, p: Dict, is_open: bool = True) -> str:
        """Форматировать одну позицию"""
        side_emoji = '🟢' if p['side'].upper() == 'LONG' else '🔴'
        side_text = 'ЛОНГ' if p['side'].upper() == 'LONG' else 'ШОРТ'

        lines = [
            f"{side_emoji} {p['symbol']} — {side_text}",
            f"   Вход: {p['entry_price']:.2f}",
        ]

        if p.get('stop_loss'):
            lines.append(f"   SL: {p['stop_loss']:.2f}")
        if p.get('take_profit'):
            lines.append(f"   TP: {p['take_profit']:.2f}")

        lines.append(f"   Объём: {p['quantity']:.4f} ({p['cost_usdt']:.2f} USDT)")

        opened = p['opened_at'].astimezone(timezone(timedelta(hours=3)))
        lines.append(f"   Открыта: {opened.strftime('%d.%m %H:%M')}")

        if is_open:
            # Get live price and calculate real PnL
            symbol = p['symbol']
            current_price = self._get_bybit_price(symbol)
            entry_price = p['entry_price']
            quantity = p['quantity']
            is_long = p['side'].upper() == 'LONG'

            if current_price > 0:
                if is_long:
                    pnl = (current_price - entry_price) * quantity
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100
                else:
                    pnl = (entry_price - current_price) * quantity
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100
            else:
                pnl = p.get('unrealized_pnl', 0)
                pnl_pct = p.get('unrealized_pnl_percent', 0)

            pnl_emoji = '📈' if pnl >= 0 else '📉'
            lines.append(f"   Текущая: {current_price:.2f}")
            lines.append(f"   PnL: {pnl_emoji} {pnl:+.2f} USDT ({pnl_pct:+.2f}%)")
        else:
            closed = p['closed_at'].astimezone(timezone(timedelta(hours=3)))
            lines.append(f"   Закрыта: {closed.strftime('%d.%m %H:%M')}")
            pnl = p.get('realized_pnl', 0)
            pnl_pct = p.get('realized_pnl_percent', 0)
            pnl_emoji = '📈' if pnl >= 0 else '📉'
            lines.append(f"   PnL: {pnl_emoji} {pnl:+.2f} USDT ({pnl_pct:+.2f}%)")

        return '\n'.join(lines)

    def format_full_report(self) -> str:
        """Форматировать полный отчёт"""
        lines = ['📊 *Trading Report*', '='*30, '']

        # Открытые позиции
        open_pos = self.get_open_positions()
        lines.append(f"📁 *Открытые позиции: {len(open_pos)}*")
        if open_pos:
            for p in open_pos[:5]:
                lines.append(self.format_position(p, is_open=True))
                lines.append('')
        else:
            lines.append('   Нет открытых позиций')
        lines.append('')

        # Закрытые за 24ч
        closed_24h = self.get_closed_positions_24h()
        lines.append(f"📅 *Закрытые за 24ч: {len(closed_24h)}*")
        if closed_24h:
            for p in closed_24h:
                lines.append(self.format_position(p, is_open=False))
                lines.append('')
        else:
            lines.append('   Нет закрытых позиций за 24ч')
        lines.append('')

        # Последние сигналы
        signals = self.get_recent_signals(limit=8)
        lines.append(f"📡 *Последние сигналы: {len(signals)}*")
        if signals:
            for s in signals[:8]:
                lines.append(self.format_signal(s))
                lines.append('')
        else:
            lines.append('   Нет сигналов')
        lines.append('')

        return '\n'.join(lines)


if __name__ == '__main__':
    # Test
    config = {
        'host': '127.0.0.1',
        'port': 5433,
        'database': 'cryptotrader',
        'user': 'andy',
        'password': 'Glumov555'
    }
    fmt = TelegramFormatter(config)
    print(fmt.format_full_report())
