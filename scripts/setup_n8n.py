import requests

# Try to access the public API that should trigger setup
N8N_URL = "http://192.168.0.91:5678"

# The user is there but with empty email
# Let's try to invoke the setup via the UI endpoint
# Or we can update directly via database but need the password hash

# Generate bcrypt hash
import bcrypt
password = "Glumov555"
hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=10))
hashed_2a = hashed.decode('utf-8').replace('$2b$', '$2a$')
print(f"Hash: {hashed_2a}")

# Update via API - try different approach
# In n8n v2, setup can be done via the owner endpoint when there's a pending owner
resp = requests.post(f"{N8N_URL}/rest/owner", json={
    "email": "gluman@yandex.ru",
    "firstName": "Andey",
    "lastName": "Glumov", 
    "password": "Glumov555"
})
print(f"Owner: {resp.status_code} - {resp.text[:200]}")