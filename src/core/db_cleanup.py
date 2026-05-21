#!/usr/bin/env python3
"""
CryptoTrader Database Cleanup System
=====================================
Automated pruning of stale/expired data from the PostgreSQL database.
Configurable retention periods per table, safe deletions (never removes
OPEN positions), transaction-safe, and logging-enabled.

Usage:
    python db_cleanup.py              # Run with defaults
    python db_cleanup.py --dry-run    # Preview what would be deleted
    python db_cleanup.py --all        # Also clean OHLCV data (risky, slow)
    python db_cleanup.py --keep-signals 14 --keep-trades 60
"""

import argparse
import logging
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import psycopg2
from psycopg2.extras import execute_values

# ─── DB Connection Config ───────────────────────────────────────────
DB_CONFIG = {
    'host': '192.168.0.149',
    'port': 5432,
    'dbname': 'cryptotrader',
    'user': 'cryptotrader',
    'password': 'cryptotrader123',
}

# ─── Default Retention Periods (days) ───────────────────────────────
DEFAULTS = {
    'signals':         7,    # Trading signals older than 7 days → delete
    'strategy_signals': 7,   # Strategy-generated signals older than 7 days → delete
    'trades':          30,   # Trade history kept for 30 days
    'decisions':       7,    # LLM decision records kept for 7 days
    'news_raw':        7,    # News articles kept for 7 days
    'agent_logs':      7,    # Agent log entries kept for 7 days
    'positions':       90,   # CLOSED positions older than 90 days → delete
    'ohlcv_raw':       30,   # Raw candles older than 30 days → delete (optional, --all)
    'ohlcv_processed': 30,   # Processed candles older than 30 days → delete (optional, --all)
    'export_history':  30,   # Export records kept for 30 days
}

# Tables cleaned only with --all flag (expensive, large data)
OPTIONAL_TABLES = {'ohlcv_raw', 'ohlcv_processed'}


def setup_logging(verbose: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger('db_cleanup')


class DBCleanup:
    """Safe, transactional database cleanup with configurable retention."""

    def __init__(self, log: logging.Logger, dry_run: bool = False,
                 keep_all: bool = False, retentions: Optional[dict] = None):
        self.log = log
        self.dry_run = dry_run
        self.keep_all = keep_all
        self.retentions = retentions or dict(DEFAULTS)
        self.stats = {}  # table -> {deleted, remaining}

    def connect(self):
        """Open PostgreSQL connection."""
        self.conn = psycopg2.connect(**DB_CONFIG)
        self.conn.autocommit = False
        self.log.debug("Connected to PostgreSQL %s:%s/%s",
                       DB_CONFIG['host'], DB_CONFIG['port'], DB_CONFIG['dbname'])

    def close(self):
        """Close connection."""
        if hasattr(self, 'conn') and self.conn:
            self.conn.close()
            self.log.debug("Connection closed")

    def _table_counts(self) -> dict:
        """Return {table: count} for all tracked tables."""
        counts = {}
        all_tables = set(self.retentions.keys())
        for t in sorted(all_tables):
            try:
                cur = self.conn.cursor()
                cur.execute("SELECT COUNT(*) FROM {}".format(t))
                counts[t] = cur.fetchone()[0]
                cur.close()
            except Exception as e:
                counts[t] = 0  # Table doesn't exist or error — treat as 0
                self.log.debug("  %s: skipped (%s)", t, str(e)[:80])
                # Rollback to clear the error state
                try:
                    self.conn.rollback()
                except Exception:
                    pass
        return counts

    def _safe_delete(self, table: str, condition: str,
                     condition_desc: str, params=()) -> int:
        """Execute DELETE with logging. Returns row count.
        Uses SAVEPOINT so errors in one table don't roll back others."""
        sql = "DELETE FROM {} WHERE {}".format(table, condition)
        if self.dry_run:
            count_sql = "SELECT COUNT(*) FROM {} WHERE {}".format(table, condition)
            try:
                cur = self.conn.cursor()
                cur.execute(count_sql, params)
                count = cur.fetchone()[0]
                cur.close()
                self.log.info("  [DRY RUN] %s: would delete %d rows (%s)", table, count, condition_desc)
                return count
            except Exception as e:
                self.log.debug("  %s: %s — skipping", table, str(e)[:80])
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return 0

        # Use savepoint so errors don't roll back other tables
        cur = self.conn.cursor()
        cur.execute("SAVEPOINT cleanup_savepoint")
        try:
            cur.execute(sql, params)
            count = cur.rowcount
            cur.execute("RELEASE SAVEPOINT cleanup_savepoint")
            cur.close()
            self.log.info("  %s: deleted %d rows (%s)", table, count, condition_desc)
            return count
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT cleanup_savepoint")
            cur.close()
            self.log.debug("  %s: %s — skipping", table, str(e)[:80])
            return 0

    def _remaining(self, table: str) -> int:
        """Count rows remaining after cleanup."""
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]
        except Exception:
            return -1
        finally:
            cur.close()

    def cleanup_signals(self, days: int):
        """Remove executed/skipped/failed signals older than N days.
        Never removes PENDING signals (they might still be executed)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = 0
        deleted += self._safe_delete(
            'signals',
            "created_at < %s AND status != 'PENDING'",
            f"status != PENDING older than {days}d",
            (cutoff,),
        )
        deleted += self._safe_delete(
            'signals',
            "created_at < %s AND status = 'PENDING' AND signal_type = 'HOLD'",
            f"PENDING HOLD signals older than {days}d",
            (cutoff,),
        )
        self.stats['signals'] = {'deleted': deleted}

    def cleanup_strategy_signals(self, days: int):
        """Remove strategy signals older than N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = self._safe_delete(
            'strategy_signals',
            "created_at < %s AND status != 'pending'",
            f"non-pending older than {days}d",
            (cutoff,),
        )
        self.stats['strategy_signals'] = {'deleted': deleted}

    def cleanup_trades(self, days: int):
        """Remove trade history older than N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = self._safe_delete(
            'trades',
            "created_at < %s",
            f"older than {days}d",
            (cutoff,),
        )
        self.stats['trades'] = {'deleted': deleted}

    def cleanup_decisions(self, days: int):
        """Remove LLM decisions older than N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = self._safe_delete(
            'decisions',
            "created_at < %s",
            f"older than {days}d",
            (cutoff,),
        )
        self.stats['decisions'] = {'deleted': deleted}

    def cleanup_news(self, days: int):
        """Remove news articles older than N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = self._safe_delete(
            'news_raw',
            "published_at < %s",
            f"published older than {days}d",
            (cutoff,),
        )
        self.stats['news_raw'] = {'deleted': deleted}

    def cleanup_agent_logs(self, days: int):
        """Remove agent log entries older than N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = self._safe_delete(
            'agent_logs',
            "timestamp < %s",
            f"older than {days}d",
            (cutoff,),
        )
        self.stats['agent_logs'] = {'deleted': deleted}

    def cleanup_positions(self, days: int):
        """Remove CLOSED positions older than N days.
        NEVER removes OPEN positions — safety critical."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Safety check: count open positions first
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM positions WHERE UPPER(status) = 'OPEN'")
        open_count = cur.fetchone()[0]
        cur.close()

        if open_count > 0:
            self.log.info("  Positions: %d OPEN positions — will NOT touch them", open_count)

        deleted = self._safe_delete(
            'positions',
            "UPPER(status) != 'OPEN' AND closed_at < %s",
            f"closed older than {days}d",
            (cutoff,),
        )

        # Also clean positions that are closed but have no closed_at (legacy)
        deleted += self._safe_delete(
            'positions',
            "UPPER(status) != 'OPEN' AND closed_at IS NULL AND created_at < %s",
            f"closed with NULL closed_at, created older than {days}d",
            (cutoff,),
        )

        self.stats['positions'] = {'deleted': deleted, 'open_preserved': open_count}

    def cleanup_ohlcv_raw(self, days: int):
        """Remove raw OHLCV candles older than N days.
        This is optional and expensive — only runs with --all."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = self._safe_delete(
            'ohlcv_raw',
            "timestamp < %s",
            f"candles older than {days}d",
            (cutoff,),
        )
        self.stats['ohlcv_raw'] = {'deleted': deleted}

    def cleanup_ohlcv_processed(self, days: int):
        """Remove processed OHLCV candles older than N days.
        Only runs with --all."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = self._safe_delete(
            'ohlcv_processed',
            "timestamp < %s",
            f"candles older than {days}d",
            (cutoff,),
        )
        self.stats['ohlcv_processed'] = {'deleted': deleted}

    def cleanup_export_history(self, days: int):
        """Remove export history records older than N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = self._safe_delete(
            'export_history',
            "created_at < %s",
            f"older than {days}d",
            (cutoff,),
        )
        self.stats['export_history'] = {'deleted': deleted}

    def vacuum(self):
        """Run VACUUM ANALYZE on cleaned tables to reclaim space.
        Must run with autocommit=True since VACUUM can't run inside a transaction."""
        if self.dry_run:
            self.log.info("  [DRY RUN] Would run VACUUM ANALYZE")
            return

        # Commit current transaction first
        self.conn.commit()

        # VACUUM requires autocommit mode
        old_autocommit = self.conn.autocommit
        self.conn.autocommit = True

        tables_to_vacuum = set(self.stats.keys())
        for t in sorted(tables_to_vacuum):
            try:
                cur = self.conn.cursor()
                cur.execute("VACUUM ANALYZE {}".format(t))
                cur.close()
                self.log.debug("  VACUUM ANALYZE %s: done", t)
            except Exception as e:
                self.log.warning("  VACUUM ANALYZE %s: %s", t, str(e)[:100])

        # Restore original autocommit setting
        self.conn.autocommit = old_autocommit

    def run(self):
        """Execute full cleanup cycle."""
        self.connect()

        self.log.info("=" * 60)
        self.log.info("DB Cleanup — Before")
        self.log.info("=" * 60)
        before = self._table_counts()
        for t, c in sorted(before.items()):
            self.log.info("  %s: %d rows", t, c)

        self.log.info("-" * 60)
        self.log.info("Cleaning...")
        self.log.info("-" * 60)

        try:
            self.cleanup_signals(self.retentions.get('signals', 7))
            self.cleanup_strategy_signals(self.retentions.get('strategy_signals', 7))
            self.cleanup_trades(self.retentions.get('trades', 30))
            self.cleanup_decisions(self.retentions.get('decisions', 7))
            self.cleanup_news(self.retentions.get('news_raw', 7))
            self.cleanup_agent_logs(self.retentions.get('agent_logs', 7))
            self.cleanup_positions(self.retentions.get('positions', 90))
            self.cleanup_export_history(self.retentions.get('export_history', 30))

            if self.keep_all:
                self.cleanup_ohlcv_raw(self.retentions.get('ohlcv_raw', 30))
                self.cleanup_ohlcv_processed(self.retentions.get('ohlcv_processed', 30))

            # Commit all deletions
            if not self.dry_run:
                self.conn.commit()
                self.log.info("Commit successful")
                self.vacuum()
            else:
                self.log.info("DRY RUN — no changes committed")
                self.conn.rollback()

        except Exception as e:
            self.conn.rollback()
            self.log.error("Cleanup failed, rolled back: %s", e)
            raise

        # After counts
        self.log.info("-" * 60)
        self.log.info("DB Cleanup — After")
        self.log.info("=" * 60)
        after = self._table_counts()
        total_deleted = 0
        for t, c in sorted(after.items()):
            b = before.get(t, 0)
            d = b - c if isinstance(b, int) and isinstance(c, int) else 0
            total_deleted += d
            self.log.info("  %s: %d rows (deleted %d)", t, c, d)

        self.log.info("-" * 60)
        self.log.info("Total rows deleted: %d", total_deleted)
        self.log.info("Mode: %s", "DRY RUN" if self.dry_run else "APPLIED")
        self.log.info("=" * 60)

        self.close()
        return total_deleted


def main():
    parser = argparse.ArgumentParser(description='CryptoTrader DB Cleanup')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview deletions without applying')
    parser.add_argument('--all', action='store_true', dest='keep_all',
                        help='Also clean OHLCV data (large, slow)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Debug logging')
    parser.add_argument('--keep-signals', type=int, default=None,
                        help='Days to keep signals (default: 7)')
    parser.add_argument('--keep-trades', type=int, default=None,
                        help='Days to keep trades (default: 30)')
    parser.add_argument('--keep-decisions', type=int, default=None,
                        help='Days to keep decisions (default: 7)')
    parser.add_argument('--keep-news', type=int, default=None,
                        help='Days to keep news (default: 7)')
    parser.add_argument('--keep-positions', type=int, default=None,
                        help='Days to keep closed positions (default: 90)')
    parser.add_argument('--keep-ohlcv', type=int, default=None,
                        help='Days to keep OHLCV data (default: 30)')

    args = parser.parse_args()

    log = setup_logging(verbose=args.verbose)

    retentions = dict(DEFAULTS)
    if args.keep_signals is not None:
        retentions['signals'] = args.keep_signals
        retentions['strategy_signals'] = args.keep_signals
    if args.keep_trades is not None:
        retentions['trades'] = args.keep_trades
    if args.keep_decisions is not None:
        retentions['decisions'] = args.keep_decisions
    if args.keep_news is not None:
        retentions['news_raw'] = args.keep_news
    if args.keep_positions is not None:
        retentions['positions'] = args.keep_positions
    if args.keep_ohlcv is not None:
        retentions['ohlcv_raw'] = args.keep_ohlcv
        retentions['ohlcv_processed'] = args.keep_ohlcv

    cleaner = DBCleanup(
        log=log,
        dry_run=args.dry_run,
        keep_all=args.keep_all,
        retentions=retentions,
    )

    try:
        total = cleaner.run()
        sys.exit(0)
    except Exception as e:
        log.error(f"Fatal: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
