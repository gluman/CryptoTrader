import subprocess
import base64

# Use docker exec to base64 encode the file
result = subprocess.run(
    ['plink', '-ssh', '-pw', 'Glumov555', 'andy@192.168.0.91', '-P', '22', 
     'docker exec rag_n8n base64 /home/node/.n8n/database.sqlite'],
    capture_output=True, text=True
)

print("stdout len:", len(result.stdout))
if result.stdout:
    data = base64.b64decode(result.stdout)
    with open(r"C:\Projects\СryptoTrader\n8n_database_backup.sqlite", "wb") as f:
        f.write(data)
    print(f"Saved {len(data)} bytes")
else:
    print("stderr:", result.stderr[:500])