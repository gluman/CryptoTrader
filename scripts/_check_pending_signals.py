import sys
sys.path.insert(0, '/home/andy/cryptotrader')
from src.core.database import DatabaseManager
from src.core.config import Config
from datetime import datetime, timedelta
config = Config()
db = DatabaseManager(config.postgresql, __import__('logging').getLogger('_check_pending_signals'))
now = datetime.now()
signals = db.get_pending_signals()
print(f'Pending signals: {len(signals)}')
for s in signals:
    age = now - s.created_at if hasattr(s, 'created_at') else 'unknown'
    age_min = age.total_seconds()/60 if isinstance(age, timedelta) else '?'
    print(f'  {s.signal_id} | {s.symbol} | {s.signal} | {s.strategy} | age={age_min:.1f}min')
positions = db.get_open_positions()
print(f'Open positions in DB: {len(positions)}')
for p in positions:
    print(f'  {p.symbol} | {p.side} | {p.size} | entry={p.entry_price}')
