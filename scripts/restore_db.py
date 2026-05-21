import subprocess
import base64

# Read local database and copy to server
with open(r"C:\Projects\СryptoTrader\n8n_database_backup.sqlite", "rb") as f:
    data = f.read()

# Save to temp file
temp_b64 = r"C:\Projects\СryptoTrader\n8n_db_temp.b64"
with open(temp_b64, "w") as f:
    f.write(base64.b64encode(data).decode('utf-8'))

# Copy to server
subprocess.run(f'pscp -pw Glumov555 {temp_b64} andy@192.168.0.91:/tmp/n8n_db.b64'.split(), capture_output=True)

# Decode and restore
subprocess.run(
    'plink -ssh -pw Glumov555 andy@192.168.0.91 -P 22 "cat /tmp/n8n_db.b64 | base64 -d > /tmp/database_new.sqlite && docker stop rag_n8n && docker cp /tmp/database_new.sqlite rag_n8n:/home/node/.n8n/database.sqlite && docker start rag_n8n"'.split(),
    capture_output=True
)
print("Database restored")