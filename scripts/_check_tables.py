import sys
sys.path.insert(0, '/home/andy')
import psycopg2

conn = psycopg2.connect(
    host='127.0.0.1',
    port=5433,
    dbname='cryptotrader',
    user='andy',
    password='Glumov555'
)
cur = conn.cursor()

# List tables
cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
tables = cur.fetchall()
print("Tables:", [t[0] for t in tables])
conn.close()
