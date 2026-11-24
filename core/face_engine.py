import atexit
from pathlib import Path
from django.conf import settings
from .semantic_engine import OfflineEngine,ModelUnavailable

MODEL_KEY='yunet-2023mar-sface-2021dec-opencv4.12-exif-v1'
DIMENSIONS=128
MINIMUM=.363
FILES={'face_detection_yunet_2023mar.onnx':232589,'face_recognition_sface_2021dec.onnx':38696353}

def readiness():
    root=Path(settings.BASE_DIR)
    return (root/'.runtime/face/Scripts/python.exe').is_file() and all((root/'models/face'/name).is_file() and (root/'models/face'/name).stat().st_size==size for name,size in FILES.items())

engine=OfflineEngine(python=Path(settings.BASE_DIR)/'.runtime/face/Scripts/python.exe',worker=Path(__file__).with_name('face_worker.py'),ready=readiness,log_name='face-worker.log')
atexit.register(engine.close)

def extract(rows):return engine.request({'images':[{'id':str(id),'path':str(path)} for id,path in rows]})['images']
