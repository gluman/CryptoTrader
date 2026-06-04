"""Test PostgreSQL connection to 192.168.0.149"""
import sys
sys.path.insert(0, '.')

from src.core.config import Config
from src.core.database import DatabaseManager
from src.core.logger import setup_logger

config = Config.load()

# Override host
config._data['postgresql']['host'] = '192.168.0.149'

logger = setup_logger('test', level='INFO')

print("Testing PostgreSQL connection to 192.168.0.149...")
db = DatabaseManager(config.postgresql, logger)

if db.test_connection():
    print("SUCCESS! Connected to PostgreSQL")
    db.create_tables()
    print("Tables created successfully")
    
    # Check tables
    from sqlalchemy import text
    with db.get_session() as session:
        result = session.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name"))
        tables = [r[0] for r in result]
        print(f"\nTables ({len(tables)}):")
        for t in tables:
            print(f"  - {t}")
else:
    print("FAILED to connect")
