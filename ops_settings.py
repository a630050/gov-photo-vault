from settings import *
from transfer_common.operations import profile
OPS=profile()
if not OPS or OPS['component']!='core':raise RuntimeError('A core operations profile is required')
DEBUG=False
SECRET_KEY=Path(OPS['secret_file']).read_text(encoding='ascii').strip()
DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':OPS['database'],'OPTIONS':{'timeout':30}}}
MEDIA_ROOT=Path(OPS['media'])
ALLOWED_HOSTS=OPS.get('allowed_hosts',['localhost','127.0.0.1'])
MIDDLEWARE.insert(0,'transfer_common.ops_django.MaintenanceMiddleware')
STORAGES={'default':{'BACKEND':'transfer_common.ops_django.GuardedStorage'}}
SESSION_COOKIE_NAME='photo_ops_session'
CSRF_COOKIE_NAME='photo_ops_csrf'
X_FRAME_OPTIONS='DENY'
MIDDLEWARE.append('django.middleware.clickjacking.XFrameOptionsMiddleware')
