"""Repeatable integration drill; creates isolated data and always stops its own service."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'.vendor'))

def main():
    folder=(ROOT/'.runtime'/('ops-drill-'+uuid.uuid4().hex[:10])).resolve()
    profile=folder/'profile.json';result={'directory':str(folder),'checks':{}}
    def command(*args):
        process=subprocess.run([sys.executable,str(ROOT/'ops.py'),*args],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1'},timeout=90)
        if process.returncode:raise RuntimeError(process.stderr[-1200:])
        return process.stdout.strip()
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    command('prepare','--profile',str(profile),'--directory',str(folder),'--port',str(port))
    command('migrate','--profile',str(profile))
    import ops
    p=ops.setup(profile)
    from django.contrib.auth.models import User
    from core.models import Department,Item,Dataset,Photo
    from django.core.files.base import ContentFile
    from django.db import connections
    from PIL import Image
    import io
    user=User.objects.create(username='drill',is_superuser=True,is_staff=True)
    department=Department.objects.create(name='維運驗證科');item=Item.objects.create(department=department,name='還原驗證')
    dataset=Dataset.objects.create(item=item,name='獨立測試資料集',creator=user)
    stream=io.BytesIO();Image.new('RGB',(16,16),'blue').save(stream,'JPEG');content=stream.getvalue()
    photo=Photo.objects.create(dataset=dataset,uploader=user,name='drill.jpg',sha=hashlib.sha256(content).hexdigest(),status='submitted')
    photo.file.save('drill.jpg',ContentFile(content));photo.preview.save('drill-preview.jpg',ContentFile(content));original=photo.file.name
    connections.close_all()
    env={**os.environ,'PYTHONUTF8':'1'}
    supervisor=subprocess.Popen([sys.executable,str(ROOT/'ops.py'),'run','--profile',str(profile)],cwd=ROOT,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    def health(path):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}',timeout=3) as response:return response.status,json.loads(response.read())
        except urllib.error.HTTPError as exc:return exc.code,json.loads(exc.read())
        except (OSError,ValueError):return 0,{}
    def wait(predicate,timeout=30):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            if supervisor.poll() is not None:raise RuntimeError('Supervisor exited unexpectedly')
            time.sleep(.25)
        raise TimeoutError('Integration drill timed out')
    media=Path(p['media']);unavailable=folder/'storage-unavailable'
    try:
        wait(lambda:health('/health/ready')[0]==200)
        result['checks']['waitress_ready']=True
        state=json.loads((folder/'run/service.json').read_text())
        if state['pid']!=supervisor.pid:raise RuntimeError('Service ownership mismatch')
        old_pid=state['children']['web']['pid']
        os.kill(old_pid,signal.SIGTERM) # only the child of the supervisor this drill just created
        def restarted():
            data=json.loads((folder/'run/service.json').read_text());web=data['children'].get('web',{})
            return web.get('pid') not in (None,old_pid) and web.get('running') and health('/health/ready')[0]==200
        wait(restarted);result['checks']['web_auto_restart']=True
        # Resolve and constrain both rename targets to this newly created drill workspace.
        if media.resolve().parent!=folder or unavailable.resolve().parent!=folder:raise ValueError('Unsafe drill path')
        media.rename(unavailable)
        try:
            assert health('/health/live')[0]==200
            assert health('/health/ready')[0]==503
            assert health('/')[0]==503
            assert not media.exists() # must never silently create substitute storage
            result['checks']['missing_storage_fails_closed']=True
        finally:unavailable.rename(media)
        wait(lambda:health('/health/ready')[0]==200)
        archive=command('backup','--profile',str(profile)).splitlines()[-1]
        command('verify','--archive',archive)
        restored=folder/'restored';command('restore','--archive',archive,'--destination',str(restored))
        with contextlib.closing(sqlite3.connect(restored/'data.sqlite3')) as db:
            assert db.execute('SELECT COUNT(*) FROM core_photo').fetchone()[0]==1
        assert (restored/'storage'/original).read_bytes()==content
        assert (media/original).read_bytes()==content
        result['checks']['snapshot_verified_restored_photo_readable']=True
        assert (folder/'run/last-backup.json').is_file()
        result['checks']['backup_status_recorded']=True
    finally:
        if unavailable.exists() and not media.exists():unavailable.rename(media)
        try:command('stop','--profile',str(profile))
        except Exception:(folder/'run/stop').touch()
        try:supervisor.wait(timeout=40)
        except subprocess.TimeoutExpired:supervisor.terminate();supervisor.wait(timeout=10);raise RuntimeError('Supervisor did not stop cleanly')
        result['checks']['service_stopped']=supervisor.returncode==0
        from transfer_common.operations import atomic_json
        atomic_json(folder/'drill-result.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
