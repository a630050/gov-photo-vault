import json,os
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parent.parent
def config(required=True):
    path=Path(os.environ.get('PHOTO_GATEWAY_CONFIG',ROOT/'.runtime/external-config.json'))
    if not path.is_file():
        if required:raise RuntimeError('請先執行 python manage.py prepare_external')
        return None
    data=json.loads(path.read_text(encoding='utf-8'))
    parsed=urlsplit(data['url'])
    if parsed.scheme not in ['http','https'] or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ['','/']:raise RuntimeError('閘道網址設定無效')
    if parsed.scheme=='http' and parsed.hostname not in ['localhost','127.0.0.1','::1']:raise RuntimeError('非本機閘道必須使用 HTTPS')
    if len(data.get('bridge_key',''))<40 or len(data.get('session_key',''))<40:raise RuntimeError('閘道密鑰設定無效')
    return data
