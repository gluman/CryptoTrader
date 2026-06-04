#!/usr/bin/env python3
"""
Migration script to add unique constraint on ohlcv_raw table
Run: python migrate_db.py
"""

import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / 'src'))

from core.config import Config
from core.database import DatabaseManager
from sqlalchemy import text

def migrate():
    config = Config.load()
    db = DatabaseManager(config.postgresql, logging.getLogger('migrate'))
    
    with db.get_session() as session:
        try:
            # Check if constraint already exists
            check_sql = """
            SELECT conname 
            FROM pg_constraint 
            WHERE conname = 'uix_ohlcv_unique' 
            AND conrelid = 'ohlcv_raw'::regclass;
            """
            result = session.execute(text(check_sql)).fetchone()
            
            if result:
                print("✓ Constraint uix_ohlcv_unique already exists")
                return
            
            # Add the unique constraint
            alter_sql = """
            ALTER TABLE ohlcv_raw 
            ADD CONSTRAINT uix_ohlcv_unique 
            UNIQUE (exchange, symbol, timeframe, timestamp);
            """
            session.execute(text(alter_sql))
            print("✓ Added unique constraint uix_ohlcv_unique on ohlcv_raw")
            
        except Exception as e:
            print(f"✗ Migration failed: {e}")
            raise

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    migrate()
