"""Deployment primitives shared by the intranet app and isolated upload gateway."""
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile

MARKER='.photo-storage-id'

def windows_lock(f,exclusive):
    # CRT _locking does not provide shared leases on Windows; use LockFileEx.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    class OVERLAPPED(ctypes.Structure):
        _fields_=[('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),('Offset',wintypes.DWORD),('OffsetHigh',wintypes.DWORD),('hEvent',wintypes.HANDLE)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(OVERLAPPED)]
    kernel.UnlockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(OVERLAPPED)]
    handle=msvcrt.get_osfhandle(f.fileno());overlap=OVERLAPPED()
    if not kernel.LockFileEx(handle,1|(2 if exclusive else 0),0,1,0,ctypes.byref(overlap)):raise ctypes.WinError(ctypes.get_last_error())
    return lambda:kernel.UnlockFileEx(handle,0,1,0,ctypes.byref(overlap))

def profile():
    path=os.environ.get('PHOTO_OPS_CONFIG')
    if not path:return None
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if data.get('version')!=1:raise ValueError('Unsupported operations profile')
    for key in ('database','media','run','backups','secret_file'):
        if not Path(data[key]).is_absolute():raise ValueError('Operations paths must be absolute')
    if str(data['database']).startswith(('\\\\','//')):raise ValueError('SQLite must be on a local disk')
    if os.name=='nt':
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(Path(data['database']).anchor))==4:raise ValueError('SQLite cannot use a mapped network drive')
    media=Path(data['media']).resolve()
    if Path(path).resolve().is_relative_to(media):raise ValueError('Operations profile must be outside photo storage')
    for key in ('database','run','backups','secret_file'):
        path=Path(data[key]).resolve()
        if path==media or path.is_relative_to(media):raise ValueError('Database, runtime, secrets and backups must be outside photo storage')
    return data

def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)

def storage_check(p,write=False):
    root=Path(p['media'])
    try:identity=(root/MARKER).read_text(encoding='ascii').strip() if root.is_dir() else None
    except (OSError,UnicodeError) as exc:raise OSError('Storage identity unavailable or mismatched') from exc
    if identity!=p['storage_id']:
        raise OSError('Storage identity unavailable or mismatched')
    if write:
        probe=root/('.probe-'+uuid.uuid4().hex);renamed=probe.with_suffix('.ok')
        try:
            with probe.open('xb') as f:f.write(b'photo-storage-probe');f.flush();os.fsync(f.fileno())
            probe.rename(renamed)
            if renamed.read_bytes()!=b'photo-storage-probe':raise OSError('Storage readback failed')
        finally:
            probe.unlink(missing_ok=True);renamed.unlink(missing_ok=True)
    free=shutil.disk_usage(root).free
    if free<int(p.get('min_free_mb',512))*1024*1024:raise OSError('Storage free space below threshold')
    return free

@contextlib.contextmanager
def gate(exclusive=False,timeout=10):
    """All deployment writers share this lock; backup obtains the exclusive lease."""
    p=profile()
    if not p:yield;return
    path=Path(p['run'])/'snapshot.lock';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as f:
        if path.stat().st_size==0:f.write(b'0');f.flush()
        deadline=time.monotonic()+timeout
        while True:
            try:
                f.seek(0)
                if os.name=='nt':
                    unlock=windows_lock(f,exclusive)
                else:
                    import fcntl
                    fcntl.flock(f,fcntl.LOCK_NB|(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH))
                break
            except OSError:
                if time.monotonic()>=deadline:raise TimeoutError('Maintenance lock busy; retry later')
                time.sleep(.1)
        try:yield
        finally:
            f.seek(0)
            if os.name=='nt':unlock()
            else:fcntl.flock(f,fcntl.LOCK_UN)

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

@contextlib.contextmanager
def work_cycle(name):
    p=profile()
    def heartbeat(state):
        if p:atomic_json(Path(p['run'])/(name+'-heartbeat.json'),{'pid':os.getpid(),'updated':time.time(),'state':state})
    try:
        with gate():
            if p:storage_check(p)
            heartbeat('working')
            yield
        heartbeat('idle')
    except Exception:
        heartbeat('error')
        raise

def check_database(path):
    with contextlib.closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('SQLite integrity check failed')
        if db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('SQLite foreign key check failed')

def check_references(database,names):
    """Make sure every persisted file reference is actually in the snapshot."""
    with contextlib.closing(sqlite3.connect(Path(database).as_uri()+'?mode=ro',uri=True)) as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table,columns in [('core_photo',('file','preview')),('core_personreference',('file',)),('core_export',('file',)),('external_gateway_upload',('file','preview'))]:
            if table not in tables:continue
            for column in columns:
                for (name,) in db.execute(f'SELECT "{column}" FROM "{table}"'):
                    if name and 'storage/'+name not in names:raise ValueError(f'Referenced file missing from backup ({table}.{column})')

def backup(database,media,destination,component='core'):
    database=Path(database).resolve();media=Path(media).resolve();destination=Path(destination).resolve()
    if destination==media or destination.is_relative_to(media):raise ValueError('Backups must be outside photo storage')
    destination.mkdir(parents=True,exist_ok=True)
    stamp=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    target=destination/f'photo-{component}-{stamp}.zip'
    partial=target.with_suffix('.partial')
    with gate(exclusive=True,timeout=60),tempfile.TemporaryDirectory(prefix='photo-snapshot-') as temp:
        p=profile()
        if p:storage_check(p)
        snapshot=Path(temp)/'data.sqlite3'
        with contextlib.closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as source,contextlib.closing(sqlite3.connect(snapshot)) as dest:source.backup(dest)
        check_database(snapshot)
        entries={'data.sqlite3':snapshot}
        def walk_error(error):raise error
        for directory,dirs,files in os.walk(media,followlinks=False,onerror=walk_error):
            for name in dirs+files:
                item=Path(directory)/name
                if item.is_symlink() or item.is_junction():raise ValueError('Storage reparse points are not supported in snapshots')
            for name in files:
                item=Path(directory)/name
                if name.startswith('.probe-'):continue
                entries['storage/'+item.relative_to(media).as_posix()]=item
        manifest={'version':2,'component':component,'created':time.time(),'files':{}}
        check_references(snapshot,set(entries))
        try:
            with zipfile.ZipFile(partial,'w',zipfile.ZIP_DEFLATED) as z:
                for name,path in entries.items():
                    manifest['files'][name]={'sha256':digest(path),'size':path.stat().st_size};z.write(path,name)
                z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
            verify(partial)
            if p:storage_check(p)
            os.replace(partial,target)
            if p:atomic_json(Path(p['run'])/'last-backup.json',{'archive':str(target),'verified':True,'created':time.time(),'sha256':digest(target)})
        finally:partial.unlink(missing_ok=True)
    return target

def verify(archive,destination=None):
    """Validate before restore: never use ZipFile.extract on untrusted paths."""
    with zipfile.ZipFile(archive) as z:
        infos=z.infolist();names=[i.filename for i in infos]
        if len(names)!=len(set(names)) or 'manifest.json' not in names:raise ValueError('Duplicate entries or missing manifest')
        for i in infos:
            name=i.filename;parts=PurePosixPath(name).parts
            if '\\' in name or ':' in name or name.startswith('/') or '..' in parts or str(PurePosixPath(name))!=name or i.is_dir() or (i.external_attr>>16)&0o170000==0o120000:
                raise ValueError('Unsafe archive entry')
        if z.getinfo('manifest.json').file_size>16*1024*1024:raise ValueError('Manifest too large')
        manifest=json.loads(z.read('manifest.json'))
        if manifest.get('version')!=2 or manifest.get('component') not in ('core','gateway'):raise ValueError('Unsupported backup format')
        records=manifest['files']
        if set(names)!=set(records)|{'manifest.json'} or 'data.sqlite3' not in records:raise ValueError('Manifest entry mismatch')
        if any(name!='data.sqlite3' and not name.startswith('storage/') for name in records):raise ValueError('Unexpected archive file')
        with tempfile.TemporaryDirectory(prefix='photo-verify-') as temp:
            for name,record in records.items():
                if z.getinfo(name).file_size!=record['size']:raise ValueError('Archive size mismatch')
                h=hashlib.sha256()
                output=Path(temp)/'data.sqlite3' if name=='data.sqlite3' else None
                with z.open(name) as src,contextlib.ExitStack() as stack:
                    dst=stack.enter_context(output.open('wb')) if output else None
                    for chunk in iter(lambda:src.read(1024*1024),b''):
                        h.update(chunk)
                        if dst:dst.write(chunk)
                if h.hexdigest()!=record['sha256']:raise ValueError('Archive checksum mismatch')
            check_database(Path(temp)/'data.sqlite3')
            check_references(Path(temp)/'data.sqlite3',set(records))
        if destination:
            dest=Path(destination).resolve()
            if dest.exists():raise ValueError('Restore destination must not exist')
            dest.parent.mkdir(parents=True,exist_ok=True)
            staging=Path(tempfile.mkdtemp(prefix='.photo-restore-',dir=dest.parent))
            try:
                for name in records:
                    out=staging/name;out.parent.mkdir(parents=True,exist_ok=True)
                    with z.open(name) as src,out.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
                (staging/'storage').mkdir(exist_ok=True)
                atomic_json(staging/'restore-manifest.json',manifest)
                staging.rename(dest)
            finally:
                if staging.exists():shutil.rmtree(staging)
    return manifest
