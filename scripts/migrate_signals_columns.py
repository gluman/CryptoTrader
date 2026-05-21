#!/usr/bin/env python3
"""
Migration script to add market_type, leverage, margin_usdt, timeframe columns to signals table
Run: python migrate_signals_columns.py
"""

import os
import sys
from pathlib import Path

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / 'src'))

from core.config import Config
from core.database import DatabaseManager
from sqlalchemy import text
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('migrate_signals_columns')

def migrate():
    config = Config.load()
    db = DatabaseManager(config.postgresql, logger)
    
    with db.get_session() as session:
        try:
            # 1. Check existing columns
            result = session.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'signals' AND column_name IN ('market_type', 'leverage', 'margin_usdt', 'timeframe')
            """)).fetchall()
            existing = {r[0] for r in result}
            logger.info(f"Existing columns in signals: {existing}")
            
            # 2. Add leverage column
            if 'leverage' not in existing:
                session.execute(text("ALTER TABLE signals ADD COLUMN leverage INTEGER DEFAULT 1"))
                logger.info("✓ Added leverage column")
            else:
                logger.info("○ leverage column already exists")
            
            # 3. Add margin_usdt column
            if 'margin_usdt' not in existing:
                session.execute(text("ALTER TABLE signals ADD COLUMN margin_usdt DECIMAL(20,8)"))
                logger.info("✓ Added margin_usdt column")
            else:
                logger.info("○ margin_usdt column already exists")
            
            # 4. Set default for timeframe if not set
            result = session.execute(text("""
                SELECT column_default FROM information_schema.columns 
                WHERE table_name = 'signals' AND column_name = 'timeframe'
            """)).fetchone()
            
            if not result or not result[0]:
                session.execute(text("ALTER TABLE signals ALTER COLUMN timeframe SET DEFAULT '1h'"))
                logger.info("✓ Set default '1h' for timeframe column")
            else:
                logger.info("○ timeframe already has default")
            
            # 5. Update existing records with NULL values
            session.execute(text("UPDATE signals SET leverage = 1 WHERE leverage IS NULL"))
            logger.info("✓ Updated NULL leverage values to 1")
            
            session.execute(text("UPDATE signals SET timeframe = '1h' WHERE timeframe IS NULL"))
            logger.info("✓ Updated NULL timeframe values to '1h'")
            
            # 6. Verify positions table has market_type
            result = session.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'positions' AND column_name = 'market_type'
            """)).fetchone()
            
            if result:
                logger.info("✓ positions table already has market_type column")
            else:
                logger.warning("! positions table is missing market_type column")
            
            session.commit()
            logger.info("Migration completed successfully!")
            
        except Exception as e:
            session.rollback()
            logger.error(f"✗ Migration failed: {e}")
            raise

if __name__ == '__main__':
    migrate()
