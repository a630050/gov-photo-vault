"""Local, reviewable deployment tooling. Run `python ops.py --help`."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'.vendor'))
from transfer_common.operations import atomic_json,profile,storage_check,backup,verify,gate

def setup(path):
    os.environ['PHOTO_OPS_CONFIG']=str(Path(path).resolve())
    p=profile()
    if p.get('gateway_config'):os.environ['PHOTO_GATEWAY_CONFIG']=p['gateway_config']
    os.environ['DJANGO_SETTINGS_MODULE']='ops_settings' if p['component']=='core' else 'gateway_ops_settings'
    import django
    django.setup()
    return p

def prepare(args):
    path=Path(args.profile).resolve();base=Path(args.directory).resolve()
    if path.exists():raise ValueError('Profile already exists; existing settings will not be overwritten')
    media=Path(args.media).resolve() if args.media else base/'storage'
    if args.media and not media.is_dir():raise ValueError('Explicit storage directory must already exist')
    if str(base).startswith(('\\\\','//')):raise ValueError('Runtime and SQLite must be on local disk')
    if (base/'data.sqlite3').exists() or (base/'secret.txt').exists():raise ValueError('Choose a new deployment directory')
    if (media/'.photo-storage-id').exists():raise ValueError('Storage already belongs to a deployment; use its existing profile')
    if media.exists() and any(media.iterdir()):raise ValueError('New deployment requires empty storage; migrate existing data separately')
    if args.component=='gateway' and not args.gateway_config:raise ValueError('Gateway requires --gateway-config')
    if not 1024<=args.port<=65535:raise ValueError('Port must be between 1024 and 65535')
    base.mkdir(parents=True,exist_ok=True);media.mkdir(parents=True,exist_ok=True)
    storage_id=str(uuid.uuid4());(media/'.photo-storage-id').write_text(storage_id,encoding='ascii')
    (base/'secret.txt').write_text(secrets.token_urlsafe(64),encoding='ascii')
    data={'version':1,'component':args.component,'database':str(base/'data.sqlite3'),'media':str(media),'run':str(base/'run'),'backups':str(base/'backups'),'secret_file':str(base/'secret.txt'),'storage_id':storage_id,'port':args.port,'min_free_mb':512,'backup_hours':24,'semantic_worker':False,'bridge_worker':False,'job_worker':True,'allowed_hosts':['localhost','127.0.0.1']}
    if args.gateway_config:data['gateway_config']=str(Path(args.gateway_config).resolve())
    atomic_json(path,data)
    print('Prepared profile:',path)

def snapshot(p):
    try:
        target=backup(p['database'],p['media'],p['backups'],p['component'])
        atomic_json(Path(p['run'])/'backup-attempt.json',{'created':time.time(),'success':True})
        return target
    except Exception:
        atomic_json(Path(p['run'])/'backup-attempt.json',{'created':time.time(),'success':False})
        raise

def run(p):
    run_dir=Path(p['run']);run_dir.mkdir(parents=True,exist_ok=True)
    # The exclusive lease prevents a second supervisor from spawning duplicate writers.
    import logging
    from logging.handlers import RotatingFileHandler
    logger=logging.getLogger('photo-ops');logger.setLevel(logging.INFO)
    handler=RotatingFileHandler(run_dir/'supervisor.log',maxBytes=2*1024*1024,backupCount=5,encoding='utf-8');logger.addHandler(handler)
    stop=run_dir/'stop'
    children={};restart={};last_backup=0
    try:
        record=json.loads((run_dir/'last-backup.json').read_text(encoding='utf-8'));last_backup=record['created']
    except (OSError,ValueError,KeyError):pass
    # A separate lock is held for the entire supervisor lifetime.
    lock=(run_dir/'supervisor.lock').open('a+b')
    if lock.tell()==0:lock.write(b'0');lock.flush()
    lock.seek(0)
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    stop.unlink(missing_ok=True)
    services={'web':['web']}
    if p['component']=='core':
        if p.get('semantic_worker'):services['semantic']=['worker','semantic_index']
        if p.get('bridge_worker'):services['bridge']=['worker','external_bridge']
        if p.get('job_worker', True):services['jobs']=['worker','job_worker']
    started=time.time();logger.info('Supervisor started')
    try:
        while not stop.exists():
            now=time.time()
            for name,command in services.items():
                child=children.get(name)
                if child and child.poll() is None:continue
                if child:
                    logger.error('%s exited with code %s',name,child.returncode)
                    children.pop(name);restart[name]=now+min(60,2**min(6,restart.get(name+'_count',0)))
                    restart[name+'_count']=restart.get(name+'_count',0)+1
                if now<restart.get(name,0):continue
                # Child output is drained into rotating logs, never an unbounded pipe/file.
                env={**os.environ,'PYTHONUTF8':'1','PYTHONUNBUFFERED':'1'}
                proc=subprocess.Popen([sys.executable,str(ROOT/'ops.py'),*command,'--profile',os.environ['PHOTO_OPS_CONFIG']],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                children[name]=proc
                import threading
                def drain(process=proc,label=name):
                    for line in process.stdout:logger.info('%s: %s',label,line.rstrip())
                threading.Thread(target=drain,daemon=True).start()
                logger.info('%s started pid=%s',name,proc.pid)
            atomic_json(run_dir/'service.json',{'pid':os.getpid(),'started':started,'updated':now,'stopped':False,'children':{name:{'pid':c.pid,'running':c.poll() is None,'restarts':restart.get(name+'_count',0)} for name,c in children.items()}})
            if now-last_backup>=max(1,float(p.get('backup_hours',24)))*3600:
                # Run snapshot separately so health, supervision and stop remain responsive.
                previous=children.get('backup')
                if not previous or previous.poll() is not None:
                    proc=subprocess.Popen([sys.executable,str(ROOT/'ops.py'),'backup','--profile',os.environ['PHOTO_OPS_CONFIG']],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    children['backup']=proc
                    threading.Thread(target=drain,args=(proc,'backup'),daemon=True).start()
                    last_backup=now # failed snapshots remain visible in logs; retry within 5 minutes
            previous=children.get('backup')
            if previous and previous.poll() is not None and previous.returncode!=0:last_backup=min(last_backup,now-max(1,float(p.get('backup_hours',24)))*3600+300)
            time.sleep(1)
    except KeyboardInterrupt:stop.touch()
    finally:
        stop.touch();logger.info('Stopping children')
        deadline=time.monotonic()+35
        for child in children.values():
            try:child.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=10)
        atomic_json(run_dir/'service.json',{'pid':os.getpid(),'updated':time.time(),'stopped':True,'children':{}})
        lock.close();logger.info('Supervisor stopped')

def web(p):
    from django.core.wsgi import get_wsgi_application
    from waitress import create_server
    import threading
    server=create_server(get_wsgi_application(),host='127.0.0.1',port=p['port'],threads=6,channel_timeout=120,max_request_body_size=55*1024*1024,expose_tracebacks=False)
    def stop_watch():
        while not (Path(p['run'])/'stop').exists():time.sleep(.5)
        server.accepting=False
        server.task_dispatcher.shutdown(timeout=30)
        from waitress import wasyncore
        wasyncore.close_all(server._map)
    threading.Thread(target=stop_watch,daemon=True).start()
    print(f"Listening on http://127.0.0.1:{p['port']}",flush=True);server.run()

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('--profile',required=True);prep.add_argument('--directory',required=True);prep.add_argument('--media');prep.add_argument('--port',type=int,default=8010);prep.add_argument('--component',choices=['core','gateway'],default='core');prep.add_argument('--gateway-config')
    for name in ('migrate','run','web','status','stop','backup','worker'):
        cmd=sub.add_parser(name)
        if name=='worker':cmd.add_argument('worker',choices=['semantic_index','external_bridge','job_worker'])
        cmd.add_argument('--profile',required=True)
    for name in ('verify','restore'):
        cmd=sub.add_parser(name);cmd.add_argument('--archive',required=True)
        if name=='restore':cmd.add_argument('--destination',required=True)
    args=parser.parse_args()
    if args.command=='prepare':prepare(args);return
    if args.command in ('verify','restore'):
        result=verify(args.archive,getattr(args,'destination',None));print(json.dumps({'verified':True,'component':result['component'],'files':len(result['files'])}));return
    p=setup(args.profile)
    if args.command=='migrate':
        storage_check(p,write=True)
        from django.core.management import call_command
        with gate(exclusive=True):call_command('migrate',interactive=False)
    elif args.command=='backup':print(snapshot(p))
    elif args.command=='status':
        from transfer_common.ops_django import report
        result=report();print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['ready'] else 2
    elif args.command=='stop':
        path=Path(p['run']);path.mkdir(parents=True,exist_ok=True);(path/'stop').touch();print('Stop requested')
    elif args.command=='run':storage_check(p,write=True);run(p)
    elif args.command=='web':web(p)
    elif args.command=='worker':
        from django.core.management import call_command
        call_command(args.worker,watch=True,stop_file=str(Path(p['run'])/'stop'))

if __name__=='__main__':
    try:sys.exit(main() or 0)
    except Exception as exc:print(f'{type(exc).__name__}: {exc}',file=sys.stderr);sys.exit(1)
