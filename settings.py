from pathlib import Path
import os
BASE_DIR=Path(__file__).resolve().parent
SECRET_KEY=os.environ.get('PHOTO_SECRET','local-development-only-photo-system')
DEBUG=True
ALLOWED_HOSTS=['localhost','127.0.0.1','testserver']
INSTALLED_APPS=['django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','core']
MIDDLEWARE=['django.middleware.security.SecurityMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware']
ROOT_URLCONF='urls'
DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':BASE_DIR/'data.sqlite3'}}
TEMPLATES=[{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[BASE_DIR/'templates'],'APP_DIRS':True}]
DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
TIME_ZONE='Asia/Taipei'
USE_TZ=True
MEDIA_ROOT=BASE_DIR/'storage'
DATA_UPLOAD_MAX_MEMORY_SIZE=55*1024*1024
FILE_UPLOAD_MAX_MEMORY_SIZE=1024*1024
