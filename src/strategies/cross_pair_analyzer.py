"""
Cross-Pair Analysis Module
Analyze ALT/BTC, ALT/ETH, and ALT/ALT relative strength.
Generate pair trade signals when divergence > threshold.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import psycopg2

logger = logging.getLogger(__name__)


@dataclass
class CrossPairStrength:
    symbol: str
    price_usdt: float
    vs_btc_1h: float
    vs_btc_24h: float
    vs_eth_1h: float
    vs_eth_24h: float
    rsi_14: float
    momentum_1h: float
    momentum_24h: float
    rank: int  # 1 = strongest


class CrossPairAnalyzer:
    """Analyze cross-pair relative strength for spot trading signals"""

    # Minimum divergence for pair trade signal
    MIN_DIVERGENCE_PCT = 1.0
    # Commission cost for round-trip pair trade (4 × 0.1%)
    PAIR_TRADE_COST_PCT = 0.4

    DEFAULT_SYMBOLS = [
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'LINKUSDT',
        'ADAUSDT', 'AVAXUSDT', 'DOGEUSDT', 'DOTUSDT', 'LTCUSDT'
    ]

    def __init__(self, bybit_api, db_conn_params: dict):
        self.api = bybit_api
        self.db = db_conn_params

    def _get_conn(self):
        return psycopg2.connect(**self.db)

    def _ensure_table(self):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cross_pair_analysis (
                    id SERIAL PRIMARY KEY,
                    base_symbol VARCHAR(20) NOT NULL,
                    quote_symbol VARCHAR(20) NOT NULL,
                    ratio NUMERIC NOT NULL,
                    change_1h NUMERIC,
                    change_24h NUMERIC,
                    rsi_14 NUMERIC,
                    momentum_1h NUMERIC,
                    momentum_24h NUMERIC,
                    vs_btc_1h NUMERIC,
                    vs_btc_24h NUMERIC,
                    vs_eth_1h NUMERIC,
                    vs_eth_24h NUMERIC,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pair_trade_signals (
                    id SERIAL PRIMARY KEY,
                    long_symbol VARCHAR(20) NOT NULL,
                    short_symbol VARCHAR(20) NOT NULL,
                    divergence_pct NUMERIC NOT NULL,
                    expected_profit_pct NUMERIC,
                    status VARCHAR(10) DEFAULT 'pending',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    closed_at TIMESTAMPTZ,
                    realized_pnl NUMERIC
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def fetch_price_changes(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict]:
        """Fetch current prices and 1h/24h changes from Bybit"""
        if symbols is None:
            symbols = self.DEFAULT_SYMBOLS

        result = {}
        for symbol in symbols:
            try:
                # Linear tickers have percentage data
                ticker = self.api.get_tickers(category='linear', symbol=symbol)
                tickers = ticker.get('result', {}).get('list', [])
                if tickers:
                    t = tickers[0]
                    result[symbol] = {
                        'price': float(t.get('lastPrice', 0)),
                        'change_1h': float(t.get('price1hPcnt', 0)) * 100,  # Already a ratio
                        'change_24h': float(t.get('price24hPcnt', 0)) * 100,
                        'volume_24h': float(t.get('turnover24h', 0)),
                        'high_24h': float(t.get('highPrice24h', 0)),
                        'low_24h': float(t.get('lowPrice24h', 0)),
                    }
            except Exception as e:
                logger.warning(f"Failed to fetch {symbol}: {e}")

        return result

    def compute_cross_strength(self, prices: Dict[str, Dict]) -> List[CrossPairStrength]:
        """Compute relative strength of each alt vs BTC and ETH"""
        btc_price = prices.get('BTCUSDT', {}).get('price', 1)
        eth_price = prices.get('ETHUSDT', {}).get('price', 1)

        strengths = []
        for symbol, data in prices.items():
            if symbol in ('BTCUSDT', 'ETHUSDT'):
                continue

            vs_btc_1h = data.get('change_1h', 0) - prices.get('BTCUSDT', {}).get('change_1h', 0)
            vs_btc_24h = data.get('change_24h', 0) - prices.get('BTCUSDT', {}).get('change_24h', 0)
            vs_eth_1h = data.get('change_1h', 0) - prices.get('ETHUSDT', {}).get('change_1h', 0)
            vs_eth_24h = data.get('change_24h', 0) - prices.get('ETHUSDT', {}).get('change_24h', 0)

            # Simple momentum score = avg(vs_btc, vs_eth) over 24h
            momentum_1h = (vs_btc_1h + vs_eth_1h) / 2
            momentum_24h = (vs_btc_24h + vs_eth_24h) / 2

            strengths.append(CrossPairStrength(
                symbol=symbol,
                price_usdt=data['price'],
                vs_btc_1h=round(vs_btc_1h, 3),
                vs_btc_24h=round(vs_btc_24h, 3),
                vs_eth_1h=round(vs_eth_1h, 3),
                vs_eth_24h=round(vs_eth_24h, 3),
                rsi_14=0,  # TODO: compute from OHLCV
                momentum_1h=round(momentum_1h, 3),
                momentum_24h=round(momentum_24h, 3),
                rank=0,
            ))

        # Sort by 24h momentum (strongest first)
        strengths.sort(key=lambda x: x.momentum_24h, reverse=True)
        for i, s in enumerate(strengths):
            s.rank = i + 1

        return strengths

    def find_pair_trade_signals(self, strengths: List[CrossPairStrength]) -> List[Dict]:
        """Find pair trade opportunities (long strongest, short weakest)"""
        if len(strengths) < 2:
            return []

        signals = []
        strongest = strengths[0]   # rank 1
        weakest = strengths[-1]    # rank N

        divergence = strongest.momentum_24h - weakest.momentum_24h
        expected_profit = divergence - self.PAIR_TRADE_COST_PCT

        if divergence >= self.MIN_DIVERGENCE_PCT:
            signals.append({
                'type': 'pair_trade',
                'long_symbol': strongest.symbol,
                'short_symbol': weakest.symbol,
                'long_price': strongest.price_usdt,
                'short_price': weakest.price_usdt,
                'divergence_pct': round(divergence, 3),
                'expected_profit_pct': round(expected_profit, 3),
                'long_strength': {
                    'vs_btc_24h': strongest.vs_btc_24h,
                    'vs_eth_24h': strongest.vs_eth_24h,
                },
                'short_weakness': {
                    'vs_btc_24h': weakest.vs_btc_24h,
                    'vs_eth_24h': weakest.vs_eth_24h,
                },
                'viable': expected_profit > 0,
            })

        # Also check 2nd strongest vs 2nd weakest
        if len(strengths) >= 4:
            s2 = strengths[1]
            w2 = strengths[-2]
            div2 = s2.momentum_24h - w2.momentum_24h
            exp2 = div2 - self.PAIR_TRADE_COST_PCT
            if div2 >= self.MIN_DIVERGENCE_PCT and exp2 > 0:
                signals.append({
                    'type': 'pair_trade',
                    'long_symbol': s2.symbol,
                    'short_symbol': w2.symbol,
                    'long_price': s2.price_usdt,
                    'short_price': w2.price_usdt,
                    'divergence_pct': round(div2, 3),
                    'expected_profit_pct': round(exp2, 3),
                    'viable': True,
                })

        return signals

    def run_full_analysis(self, symbols: Optional[List[str]] = None) -> Dict:
        """Run complete cross-pair analysis"""
        self._ensure_table()

        # 1. Fetch prices
        prices = self.fetch_price_changes(symbols)
        if not prices:
            return {'error': 'No price data fetched'}

        # 2. Compute strength
        strengths = self.compute_cross_strength(prices)

        # 3. Find signals
        signals = self.find_pair_trade_signals(strengths)

        # 4. Save to DB
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            for s in strengths:
                cur.execute("""
                    INSERT INTO cross_pair_analysis
                    (base_symbol, quote_symbol, ratio, vs_btc_1h, vs_btc_24h,
                     vs_eth_1h, vs_eth_24h, momentum_1h, momentum_24h)
                    VALUES (%s, 'USDT', %s, %s, %s, %s, %s, %s, %s)
                """, (
                    s.symbol, s.price_usdt,
                    s.vs_btc_1h, s.vs_btc_24h,
                    s.vs_eth_1h, s.vs_eth_24h,
                    s.momentum_1h, s.momentum_24h
                ))
            conn.commit()
        finally:
            conn.close()

        return {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'symbols_analyzed': len(strengths),
            'ranking': [
                {
                    'rank': s.rank, 'symbol': s.symbol,
                    'price': s.price_usdt,
                    'vs_btc_24h': f"{s.vs_btc_24h:+.2f}%",
                    'vs_eth_24h': f"{s.vs_eth_24h:+.2f}%",
                    'momentum': f"{s.momentum_24h:+.2f}%",
                }
                for s in strengths
            ],
            'pair_trade_signals': signals,
        }
