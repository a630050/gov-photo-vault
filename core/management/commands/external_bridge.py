import time
from pathlib import Path
from django.core.management.base import BaseCommand
from django.db import close_old_connections,OperationalError
from transfer_common.operations import work_cycle,profile
from core.external_upload import bridge_once,BridgeUnavailable

class Command(BaseCommand):
    help='Internal pull worker: publish grants, fetch pending uploads, revalidate and import drafts.'
    def add_arguments(self,parser):
        parser.add_argument('--watch',action='store_true');parser.add_argument('--stop-file',default='')
    def handle(self,*args,**options):
        stop=Path(options['stop_file']) if options['stop_file'] else None
        try:
            while True:
                if stop and stop.exists():break
                close_old_connections()
                try:
                    with work_cycle('bridge'):
                        grants,uploads=bridge_once()
                    if uploads:self.stdout.write(f'已處理 {uploads} 筆外部接收項目')
                except (BridgeUnavailable,OperationalError,ValueError,KeyError,TypeError,OSError,TimeoutError):self.stderr.write('接收閘道或資料暫不可用；保留待辦並等待重試。')
                if not options['watch']:break
                time.sleep(2)
        except KeyboardInterrupt:self.stdout.write('已停止內網拉取程序')
        finally:
            if stop and stop.exists() and not profile():stop.unlink()
