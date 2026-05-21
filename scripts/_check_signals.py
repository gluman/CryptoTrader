import sys
sys.path.insert(0, '/home/andy')
import psycopg2
from datetime import datetime, timezone, timedelta

conn = psycopg2.connect(
    host='127.0.0.1',
    port=5433,
    dbname='cryptotrader',
    user='andy',
    password='Glumov555'
)
cur = conn.cursor()

threshold = datetime.now(timezone.utc) - timedelta(minutes=30)

cur.execute("""
    SELECT id, symbol, strategy, action, confidence, entry_price,
           stop_loss, take_profit, status, created_at
    FROM strategy_signals
    WHERE UPPER(status) = 'PENDING'
    ORDER BY created_at DESC
    LIMIT 20
""")

rows = cur.fetchall()
print(f"Pending signals: {len(rows)}")
now_utc = datetime.now(timezone.utc)
for r in rows:
    age_min = (now_utc - r[9]).total_seconds() / 60
    print(f"  ID={r[0]} | {r[1]} {r[3]} | {r[2]} | conf={r[4]} | created={r[9]} | age={age_min:.1f}min")

# Check stuck
stuck = [(r[0], r[1], r[9], r[3]) for r in rows if r[9] < threshold]
if stuck:
    print(f"\n⚠️ STUCK PENDING SIGNALS (>{30} min): {len(stuck)}")
    for s in stuck:
        age = (datetime.now(timezone.utc) - s[2]).total_seconds() / 60
        print(f"  ID={s[0]} | {s[1]} {s[3]} | age={age:.1f}min | created={s[2]}")
else:
    print(f"\n✅ No stuck pending signals")

conn.close()
