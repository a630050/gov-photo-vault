from gateway_settings import *
from transfer_common.operations import profile
OPS=profile()
if not OPS or OPS['component']!='gateway':raise RuntimeError('A gateway operations profile is required')
DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':OPS['database'],'OPTIONS':{'timeout':30}}}
MEDIA_ROOT=Path(OPS['media'])
MIDDLEWARE.insert(0,'transfer_common.ops_django.MaintenanceMiddleware')
STORAGES={'default':{'BACKEND':'transfer_common.ops_django.GuardedStorage'}}
