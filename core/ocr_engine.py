"""Isolated, offline RapidOCR process. No photos or queries leave the machine."""
import atexit
from functools import lru_cache
from pathlib import Path
from django.conf import settings
from .semantic_engine import ModelUnavailable, OfflineEngine

MODEL_KEY = 'rapidocr-onnxruntime-1.4.4-s2t-v1'


def python_path():
    return Path(settings.BASE_DIR) / '.runtime/ocr/Scripts/python.exe'


def readiness():
    return python_path().is_file()


engine = OfflineEngine(
    python=None,
    worker=Path(__file__).with_name('ocr_worker.py'),
    ready=readiness,
    log_name='ocr-worker.log',
)
atexit.register(engine.close)


def _get_engine():
    engine.python = python_path()
    return engine


@lru_cache(maxsize=256)
def normalize_query(text):
    return _get_engine().request({'normalize': text})['text']


def ocr_images(photos):
    return _get_engine().request({'images': [{'id': p.id, 'path': p.file.path} for p in photos]})['images']
