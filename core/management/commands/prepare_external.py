import json,secrets
from django.conf import settings
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help='Prepare independent local upload gateway configuration, without exposing network ports.'
    def handle(self,*args,**options):
        root=settings.BASE_DIR;path=root/'.runtime/external-config.json';path.parent.mkdir(exist_ok=True)
        if not path.exists():
            data={'url':'http://127.0.0.1:8002','bridge_key':secrets.token_urlsafe(48),'session_key':secrets.token_urlsafe(48),'allowed_hosts':['localhost','127.0.0.1','testserver']}
            with path.open('x',encoding='utf-8') as f:json.dump(data,f,indent=2)
        (root/'.external-gateway').mkdir(exist_ok=True)
        self.stdout.write('本機閘道設定已備妥；密鑰不輸出至畫面，服務尚未對外發布。')
