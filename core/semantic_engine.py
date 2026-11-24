"""Isolated, offline model process. No photos or queries leave the machine."""
import atexit
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from functools import lru_cache
from django.conf import settings

MODEL_KEY='chinese-clip-rn50-717ba215-t2s-exif-v1'
DIMENSIONS=1024
WEIGHT_SHA='b196ee3ee528b70be1158ab1aafb1d2f1c801ad2d9ffb3bae31b0d305f82fc88'

class ModelUnavailable(Exception):pass

def paths():
    root=Path(settings.BASE_DIR)
    return root/'.runtime/semantic/Scripts/python.exe',root/'models/chinese-clip-rn50/clip_cn_rn50.pt'

def readiness():
    python,weights=paths()
    return python.is_file() and weights.is_file() and weights.stat().st_size==308316425

class OfflineEngine:
    def __init__(self,python=None,worker=None,ready=None,log_name='semantic-worker.log'):
        self.lock=threading.Lock();self.process=None;self.responses=None;self.log=None
        self.python=python;self.worker=worker;self.ready=ready;self.log_name=log_name
    def close(self):
        if self.process and self.process.poll() is None:
            self.process.kill();self.process.wait(timeout=10)
        self.process=None
        if self.log:self.log.close();self.log=None
    def start(self):
        if self.process and self.process.poll() is None:return
        self.close()
        if not (self.ready or readiness)():raise ModelUnavailable('離線模型尚未準備完成，請檢查專案模型環境與權重')
        python=self.python or paths()[0];self.responses=queue.Queue()
        env={**os.environ,'PHOTO_MEDIA_ROOT':str(Path(settings.MEDIA_ROOT).resolve()),'PYTHONUTF8':'1','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2'}
        self.log=open(Path(settings.BASE_DIR)/'.runtime'/self.log_name,'a',encoding='utf-8')
        self.process=subprocess.Popen([str(python),'-u',str(self.worker or Path(__file__).with_name('semantic_worker.py'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        stream=self.process.stdout;responses=self.responses
        def read():
            try:
                for line in stream:
                    try:responses.put(json.loads(line))
                    except ValueError:continue
            finally:responses.put({'error':'模型程序已停止，請查看本機模型記錄'})
        threading.Thread(target=read,daemon=True).start()
    def request(self,body):
        if not self.lock.acquire(timeout=2):raise ModelUnavailable('模型忙碌中，請稍後重試')
        try:
            self.start()
            try:
                self.process.stdin.write(json.dumps(body,ensure_ascii=False)+'\n');self.process.stdin.flush()
                result=self.responses.get(timeout=90)
            except (OSError,queue.Empty):self.close();raise ModelUnavailable('模型回應逾時或中斷，請稍後重試')
            if 'error' in result:raise ModelUnavailable(result['error'])
            return result
        finally:self.lock.release()

engine=OfflineEngine()
atexit.register(engine.close)

@lru_cache(maxsize=128)
def text_vector(text):return engine.request({'text':text})['vector']

def image_vectors(photos):return engine.request({'images':[{'id':p.id,'path':p.file.path} for p in photos]})['images']
