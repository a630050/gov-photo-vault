import time
from pathlib import Path
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import connection
from django.http import JsonResponse
from .operations import profile,storage_check,gate

class GuardedStorage(FileSystemStorage):
    def path(self,name):
        p=profile()
        if p:storage_check(p)
        return super().path(name)

class MaintenanceMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        if request.path.startswith('/health/') or request.path=='/ops/status':return self.get_response(request)
        try:
            with gate():
                p=profile()
                if p:storage_check(p)
                return self.get_response(request)
        except (OSError,TimeoutError):return JsonResponse({'error':'儲存設備或維護鎖暫時無法使用，請稍後重試。'},status=503)

def report():
    p=profile();checks={};free=None
    try:
        if p and (not Path(p['database']).is_file() or Path(p['database']).stat().st_size==0):raise OSError('Database missing')
        with connection.cursor() as cursor:cursor.execute('SELECT 1 FROM django_migrations LIMIT 1');cursor.fetchone()
        checks['database']=True
    except Exception:checks['database']=False
    try:
        with gate(timeout=.2):
            if p:free=storage_check(p,write=True)
            else:
                root=Path(settings.MEDIA_ROOT)
                if not root.is_dir():raise OSError('Storage missing')
        checks['storage']=True
    except (OSError,ValueError,TimeoutError):checks['storage']=False
    result={'ready':all(checks.values()),'checks':checks,'deployment_profile':bool(p),'free_bytes':free}
    if p:
        import json
        result['component']=p['component'];result['media']=p['media'];result['database']=p['database']
        for name in ('service','last-backup','backup-attempt'):
            try:result[name]=json.loads((Path(p['run'])/(name+'.json')).read_text(encoding='utf-8'))
            except (OSError,ValueError):result[name]=None
        if result['service']:result['service']['stale']=time.time()-result['service'].get('updated',0)>30
        last=result['last-backup']
        result['backup_overdue']=not last or time.time()-last.get('created',0)>max(1,float(p.get('backup_hours',24)))*3600+300
        result['workers']={}
        for name,key in [('semantic','semantic_worker'),('bridge','bridge_worker')]:
            if not p.get(key):continue
            try:
                item=json.loads((Path(p['run'])/(name+'-heartbeat.json')).read_text(encoding='utf-8'))
                item['stale']=time.time()-item.get('updated',0)>180
            except (OSError,ValueError):item={'state':'unknown','stale':True}
            result['workers'][name]=item
    return result

def live(request):return JsonResponse({'status':'alive'})
def ready(request):
    result=report()
    return JsonResponse({'status':'ready' if result['ready'] else 'unavailable'},status=200 if result['ready'] else 503)
def status(request):
    if not getattr(request,'user',None) or not request.user.is_authenticated:return JsonResponse({'error':'請先登入'},status=401)
    if not request.user.is_superuser:return JsonResponse({'error':'需要系統管理者權限'},status=403)
    return JsonResponse(report())
