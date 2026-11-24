"""Run using the project's Python 3.12 OCR environment, never Django's interpreter."""
import contextlib
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def initialize():
    import numpy as np
    from opencc import OpenCC
    from PIL import Image, ImageOps
    from rapidocr_onnxruntime import RapidOCR

    def offline(*args, **kwargs):
        raise RuntimeError('離線 OCR 模型禁止網路連線')

    socket.socket.connect = offline
    socket.create_connection = offline

    ocr = RapidOCR()
    convert = OpenCC('s2t')

    def infer(request):
        if 'normalize' in request:
            return {'text': convert.convert(str(request['normalize']))}
        result = []
        media_root = Path(os.environ.get('PHOTO_MEDIA_ROOT', str(ROOT / 'storage'))).resolve()
        for row in request.get('images', []):
            path = Path(row['path']).resolve()
            if not path.is_relative_to(media_root):
                raise ValueError('圖片不在系統儲存範圍')
            try:
                with Image.open(path) as image:
                    rgb = ImageOps.exif_transpose(image).convert('RGB')
                    rgb.thumbnail((2400, 2400))
                    arr = np.array(rgb)
                res, _ = ocr(arr)
                lines = [convert.convert(str(item[1]).strip()) for item in (res or []) if len(item) >= 2 and str(item[1]).strip()]
                result.append({'id': row['id'], 'text': '\n'.join(lines)})
            except (OSError, ValueError, Image.DecompressionBombError):
                result.append({'id': row['id'], 'error': '無法讀取或解碼原始照片'})
        return {'images': result}

    return infer


if __name__ == '__main__':
    try:
        with contextlib.redirect_stdout(sys.stderr):
            infer = initialize()
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'error': '離線 OCR 模型無法載入，請查看本機模型記錄'}), flush=True)
        sys.exit(1)

    for line in sys.stdin:
        try:
            request = json.loads(line)
            print(json.dumps(infer(request), ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False), flush=True)
