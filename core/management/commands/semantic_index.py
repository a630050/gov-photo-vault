import time
from pathlib import Path
from django.core.management.base import BaseCommand
from django.db import close_old_connections,OperationalError
from transfer_common.operations import work_cycle,profile
from core.semantic import index_batch
from core.semantic_engine import ModelUnavailable

class Command(BaseCommand):
    help='Build offline photo-content index; --watch indexes newly submitted photos locally.'
    def add_arguments(self,parser):
        parser.add_argument('--watch',action='store_true')
        parser.add_argument('--retry',action='store_true')
        parser.add_argument('--stop-file',default='')
    def handle(self,*args,**options):
        retry=options['retry']
        stop=Path(options['stop_file']) if options['stop_file'] else None
        try:
            while True:
                if stop and stop.exists():break
                close_old_connections()
                try:
                    with work_cycle('semantic'):
                        count=index_batch(retry=retry);retry=False
                    if count:self.stdout.write(f'已處理 {count} 張照片')
                    if not count and not options['watch']:break
                except (ModelUnavailable,OperationalError,OSError,TimeoutError) as e:
                    self.stderr.write(str(e))
                    if not options['watch']:break
                    time.sleep(5)
                if options['watch']:time.sleep(2)
        except KeyboardInterrupt:self.stdout.write('已停止內容索引')
        finally:
            if stop and stop.exists() and not profile():stop.unlink()
