"""
TradingView Analyst Monitor
Scrapes analyst ideas from TradingView for BTC + altcoin predictions.
Tracks accuracy over time and generates reliability scores.
"""

import json
import re
import time
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass

import psycopg2
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


@dataclass
class AnalystPrediction:
    analyst: str
    title: str
    symbol: str
    direction: str     # 'bullish', 'bearish', 'neutral'
    target_price: Optional[float]
    timeframe: str     # '1h', '4h', '1D', '1W'
    published_at: str
    likes: int
    comments: int
    url: str


@dataclass
class AnalystScore:
    analyst: str
    followers: int
    total_predictions: int
    correct: int
    partial: int
    wrong: int
    accuracy_pct: float
    last_updated: str


class TradingViewMonitor:
    """Monitor TradingView analyst predictions and track accuracy"""

    # Top analysts to track (from our research June 2026)
    DEFAULT_ANALYSTS = [
        'MasterAnanda',      # 145K, multi-alt, bullish long-term
        'TradingShot',       # 93K, ETH bearish
        'VincePrince',       # 39K, SOL analysis
        'Cryptollica',       # 8.5K, contrarian multi-alt
        'behdark',           # 11.5K, ETH/AVAX/DOT
        'EXCAVO',            # BTC structure analysis
        'TheSignalyst',      # 75K, multi-crypto
        'MyCryptoParadise',  # 7.3K, LINK/ADA/AVAX
        'CryptoNuclear',     # 2.9K, mid-cap alts
    ]

    # Symbols to track
    TRACKED_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT',
                       'LINKUSDT', 'ADAUSDT', 'AVAXUSDT']

    def __init__(self, db_conn_params: dict):
        self.db = db_conn_params

    def _get_conn(self):
        return psycopg2.connect(**self.db)

    def _ensure_table(self):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tv_analysts (
                    username VARCHAR(50) PRIMARY KEY,
                    followers INT DEFAULT 0,
                    reputation NUMERIC DEFAULT 0,
                    accuracy_pct NUMERIC DEFAULT 0,
                    total_tracked INT DEFAULT 0,
                    correct INT DEFAULT 0,
                    partial INT DEFAULT 0,
                    wrong INT DEFAULT 0,
                    last_scraped TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tv_predictions (
                    id SERIAL PRIMARY KEY,
                    analyst VARCHAR(50) NOT NULL,
                    title TEXT,
                    symbol VARCHAR(20),
                    direction VARCHAR(10),
                    target_price NUMERIC,
                    timeframe VARCHAR(5),
                    published_at TIMESTAMPTZ,
                    scraped_at TIMESTAMPTZ DEFAULT NOW(),
                    likes INT DEFAULT 0,
                    comments INT DEFAULT 0,
                    url TEXT,
                    outcome VARCHAR(10),
                    verified_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_tv_pred_analyst ON tv_predictions(analyst)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_tv_pred_symbol ON tv_predictions(symbol)
            """)
            conn.commit()
        finally:
            conn.close()

    def scrape_analyst_ideas(self, username: str, limit: int = 10) -> List[Dict]:
        """Scrape recent ideas from a TradingView analyst profile page.
        Uses server-rendered JSON embedded in the HTML page."""

        url = f"https://www.tradingview.com/u/{username}/"
        ideas = []

        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml',
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode('utf-8', errors='ignore')

            # Extract embedded JSON data from the page
            # TradingView embeds ideas data in a script tag
            json_patterns = [
                r'window\.__DATA__\s*=\s*({.*?});',
                r'data-ideas="({.*?})"',
                r'"ideas":\s*(\[.*?\])',
            ]

            for pattern in json_patterns:
                matches = re.findall(pattern, html, re.DOTALL)
                if matches:
                    try:
                        data = json.loads(matches[0].replace('&quot;', '"'))
                        if isinstance(data, list):
                            ideas = data
                        elif isinstance(data, dict):
                            ideas = data.get('ideas', data.get('items', []))
                        break
                    except json.JSONDecodeError:
                        continue

            # Fallback: parse idea titles/directions from HTML
            if not ideas:
                # Find idea cards in HTML
                title_matches = re.findall(
                    r'data-title="([^"]+)"', html
                )
                direction_matches = re.findall(
                    r'data-direction="([^"]+)"', html
                )
                symbol_matches = re.findall(
                    r'data-symbol="([^"]+)"', html
                )

                for i in range(min(len(title_matches), limit)):
                    ideas.append({
                        'title': title_matches[i],
                        'direction': direction_matches[i] if i < len(direction_matches) else 'neutral',
                        'symbol': symbol_matches[i] if i < len(symbol_matches) else '',
                    })

            # Also try the TradingView API endpoint
            if not ideas:
                ideas = self._try_api_endpoint(username, limit)

        except urllib.error.HTTPError as e:
            logger.warning(f"HTTP error scraping {username}: {e.code}")
        except Exception as e:
            logger.warning(f"Error scraping {username}: {e}")

        return ideas[:limit]

    def _try_api_endpoint(self, username: str, limit: int) -> List[Dict]:
        """Try TradingView's internal API for user ideas"""
        url = f"https://www.tradingview.com/api/v1/u/{username}/published_ideas/?limit={limit}"
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
                'Accept': 'application/json',
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return data.get('results', data if isinstance(data, list) else [])
        except Exception:
            return []

    def scrape_symbol_ideas(self, symbol: str, limit: int = 20) -> List[Dict]:
        """Scrape top ideas for a specific symbol from TradingView"""
        # Convert BTCUSDT → BTC
        tv_symbol = symbol.replace('USDT', 'USD')
        url = f"https://www.tradingview.com/symbols/{tv_symbol}/ideas/"

        ideas = []
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode('utf-8', errors='ignore')

            # Parse idea data from HTML
            # Look for embedded JSON or structured data
            idea_blocks = re.findall(
                r'"title":"([^"]+)".*?"authorUsername":"([^"]+)".*?"pairName":"([^"]*)"'
                r'.*?"label":"([^"]*)".*?"likeCount":(\d+).*?"commentCount":(\d+)',
                html, re.DOTALL
            )

            for block in idea_blocks[:limit]:
                title, author, pair, label, likes, comments = block
                direction = 'neutral'
                if 'long' in label.lower() or 'buy' in label.lower():
                    direction = 'bullish'
                elif 'short' in label.lower() or 'sell' in label.lower():
                    direction = 'bearish'

                ideas.append({
                    'title': title,
                    'analyst': author,
                    'symbol': symbol,
                    'direction': direction,
                    'likes': int(likes),
                    'comments': int(comments),
                })

            # Fallback: simpler pattern
            if not ideas:
                titles = re.findall(r'class="tv-widget-idea__title"[^>]*>([^<]+)<', html)
                authors = re.findall(r'class="tv-widget-idea__author[^"]*"[^>]*>([^<]+)<', html)
                for i in range(min(len(titles), limit)):
                    title_text = titles[i].strip()
                    direction = 'neutral'
                    if any(w in title_text.lower() for w in ['long', 'buy', 'bullish', 'breakout', 'pump']):
                        direction = 'bullish'
                    elif any(w in title_text.lower() for w in ['short', 'sell', 'bearish', 'crash', 'dump']):
                        direction = 'bearish'
                    ideas.append({
                        'title': title_text,
                        'analyst': authors[i].strip() if i < len(authors) else 'unknown',
                        'symbol': symbol,
                        'direction': direction,
                    })

        except Exception as e:
            logger.warning(f"Error scraping {symbol} ideas: {e}")

        return ideas

    def save_predictions(self, predictions: List[Dict]):
        """Save scraped predictions to DB"""
        self._ensure_table()
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            for p in predictions:
                cur.execute("""
                    INSERT INTO tv_predictions (analyst, title, symbol, direction, target_price,
                                                timeframe, likes, comments, url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (
                    p.get('analyst', ''), p.get('title', ''),
                    p.get('symbol', ''), p.get('direction', ''),
                    p.get('target_price'), p.get('timeframe', ''),
                    p.get('likes', 0), p.get('comments', 0),
                    p.get('url', '')
                ))
            conn.commit()
            logger.info(f"Saved {len(predictions)} predictions")
        finally:
            conn.close()

    def verify_predictions(self, current_prices: Dict[str, float], age_hours: int = 48):
        """Verify past predictions against current prices"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            # Get unverified predictions older than age_hours
            cur.execute("""
                SELECT id, analyst, symbol, direction, target_price, published_at, scraped_at
                FROM tv_predictions
                WHERE outcome IS NULL
                  AND scraped_at < NOW() - INTERVAL '%s hours'
                ORDER BY scraped_at ASC
                LIMIT 100
            """, (age_hours,))

            predictions = cur.fetchall()
            verified = 0

            for pred_id, analyst, symbol, direction, target_price, published_at, scraped_at in predictions:
                symbol_usdt = symbol if symbol.endswith('USDT') else f"{symbol}USDT"
                current_price = current_prices.get(symbol_usdt)
                if not current_price:
                    continue

                # Determine if prediction was correct
                if direction == 'bullish':
                    outcome = 'correct' if current_price > float(target_price or 0) else 'wrong'
                elif direction == 'bearish':
                    outcome = 'correct' if current_price < float(target_price or 999999) else 'wrong'
                else:
                    outcome = 'partial'

                cur.execute("""
                    UPDATE tv_predictions SET outcome=%s, verified_at=NOW() WHERE id=%s
                """, (outcome, pred_id))
                verified += 1

            conn.commit()
            logger.info(f"Verified {verified}/{len(predictions)} predictions")
            return verified
        finally:
            conn.close()

    def get_analyst_scores(self) -> List[Dict]:
        """Get reliability scores for all tracked analysts"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT analyst,
                       COUNT(*) as total,
                       SUM(CASE WHEN outcome='correct' THEN 1 ELSE 0 END) as correct,
                       SUM(CASE WHEN outcome='partial' THEN 1 ELSE 0 END) as partial,
                       SUM(CASE WHEN outcome='wrong' THEN 1 ELSE 0 END) as wrong,
                       ROUND(
                           100.0 * SUM(CASE WHEN outcome='correct' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0),
                           1
                       ) as accuracy_pct
                FROM tv_predictions
                WHERE outcome IS NOT NULL
                GROUP BY analyst
                ORDER BY accuracy_pct DESC NULLS LAST
            """)
            rows = cur.fetchall()
            return [
                {
                    'analyst': r[0], 'total': r[1], 'correct': r[2],
                    'partial': r[3], 'wrong': r[4], 'accuracy': float(r[5]) if r[5] else 0
                }
                for r in rows
            ]
        finally:
            conn.close()

    def run_scrape_cycle(self) -> Dict:
        """Run full scrape cycle for all tracked analysts and symbols"""
        self._ensure_table()
        all_predictions = []

        # Scrape per analyst
        for analyst in self.DEFAULT_ANALYSTS:
            ideas = self.scrape_analyst_ideas(analyst, limit=5)
            for idea in ideas:
                idea.setdefault('analyst', analyst)
                all_predictions.append(idea)
            time.sleep(1)  # Rate limiting

        # Scrape per symbol
        for symbol in self.TRACKED_SYMBOLS:
            ideas = self.scrape_symbol_ideas(symbol, limit=5)
            all_predictions.extend(ideas)
            time.sleep(1)

        # Save all
        if all_predictions:
            self.save_predictions(all_predictions)

        # Verify old predictions
        from src.gateways.bybit_api import BybitAPI
        from src.core.config import Config
        config = Config()
        api = BybitAPI(config.bybit['api_key'], config.bybit['api_secret'])

        prices = {}
        for symbol in self.TRACKED_SYMBOLS:
            ticker = api.get_ticker(symbol)
            prices[symbol] = float(ticker.get('lastPrice', 0))

        verified = self.verify_predictions(prices)
        scores = self.get_analyst_scores()

        return {
            'scraped': len(all_predictions),
            'verified': verified,
            'analyst_scores': scores,
        }
