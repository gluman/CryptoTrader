import requests, json
url = 'http://localhost:9380'
api = 'ragflow-OJRiJt8hfSNSnIWgkRqhhz3kLmOO38qiRWqnpZ_8exs'
headers = {'Authorization': 'Bearer ' + api, 'Content-Type': 'application/json'}

# Get tenant ID from existing datasets
r = requests.get(url + '/api/v1/datasets', headers=headers)
data = r.json()
datasets = data.get('data', [])
tenant_id = None
if datasets:
    tenant_id = datasets[0].get('created_by')
print('Tenant ID:', tenant_id)

# Register gluman user
if tenant_id:
    user_data = {
        'email': 'gluman@example.com',
        'nickname': 'gluman',
        'password': 'Glumov555',
        'tenant_id': tenant_id
    }
    r = requests.post(url + '/v1/user/register', json=user_data)
    print('Register status:', r.status_code)
    print('Response:', r.text)
