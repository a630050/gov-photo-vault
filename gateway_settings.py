from pathlib import Path
from transfer_common.config import config

BASE_DIR=Path(__file__).resolve().parent
GATEWAY_CONFIG=config()
SECRET_KEY=GATEWAY_CONFIG['session_key']
DEBUG=False
ALLOWED_HOSTS=GATEWAY_CONFIG.get('allowed_hosts',['127.0.0.1','localhost','testserver'])
INSTALLED_APPS=['django.contrib.sessions','external_gateway']
MIDDLEWARE=['django.middleware.security.SecurityMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF='external_gateway.urls'
DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':BASE_DIR/'.external-gateway/gateway.sqlite3'}}
TEMPLATES=[{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[BASE_DIR/'external_gateway/templates'],'APP_DIRS':True}]
DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
USE_TZ=True
TIME_ZONE='Asia/Taipei'
MEDIA_ROOT=BASE_DIR/'.external-gateway/files'
DATA_UPLOAD_MAX_MEMORY_SIZE=55*1024*1024
FILE_UPLOAD_MAX_MEMORY_SIZE=1024*1024
SESSION_COOKIE_NAME='photo_gateway_session'
CSRF_COOKIE_NAME='photo_gateway_csrf'
SESSION_COOKIE_HTTPONLY=True
SESSION_COOKIE_SAMESITE='Lax'
SESSION_COOKIE_SECURE=GATEWAY_CONFIG['url'].startswith('https:')
CSRF_COOKIE_SECURE=SESSION_COOKIE_SECURE
CSRF_FAILURE_VIEW='external_gateway.views.csrf_failure'
X_FRAME_OPTIONS='DENY'
SECURE_CONTENT_TYPE_NOSNIFF=True
SECURE_REFERRER_POLICY='no-referrer'
SECURE_SSL_REDIRECT=SESSION_COOKIE_SECURE
if GATEWAY_CONFIG.get('trust_proxy',False):SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https')
