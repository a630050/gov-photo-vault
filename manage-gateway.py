import sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'.vendor'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','gateway_settings')
from django.conf import settings
if settings.ROOT_URLCONF!='external_gateway.urls' or any(app=='core' or app.startswith('core.') for app in settings.INSTALLED_APPS):
    raise RuntimeError('接收閘道拒絕載入內網核心設定；請檢查 DJANGO_SETTINGS_MODULE')
from django.core.management import execute_from_command_line
execute_from_command_line(sys.argv)
