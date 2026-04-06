import sys
sys.path.insert(0, '/ragflow')
from api.db.db_models import init_web_db
from api.db.services.user_service import UserService
from api.db.services.tenant_service import TenantService
from api.db.services.user_tenant_service import UserTenantService
from api.utils import get_uuid

init_web_db()

tenants = TenantService.query()
print('Tenants:', len(tenants))
for t in tenants:
    print(f'  {t.id}: {t.name}')

users = UserService.query()
print('Users:', len(users))
for u in users:
    print(f'  {u.id}: {u.email} ({u.nickname})')
