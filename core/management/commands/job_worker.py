import os
import time
import uuid
from pathlib import Path
from django.core.management.base import BaseCommand
from django.db import close_old_connections, OperationalError
from transfer_common.operations import profile, work_cycle
from core.jobs import process_one_job


class Command(BaseCommand):
    help = 'Run unified background job worker (--watch for continuous polling, --once to process current queue).'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true')
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--stop-file', default='')
        parser.add_argument('--worker-id', default='')

    def handle(self, *args, **options):
        stop = Path(options['stop_file']) if options['stop_file'] else None
        worker_id = options['worker_id'] or f'jobworker-{os.getpid()}-{uuid.uuid4().hex[:6]}'
        processed = 0
        try:
            while True:
                if stop and stop.exists():
                    break
                close_old_connections()
                try:
                    with work_cycle('background_jobs'):
                        job = process_one_job(worker_id=worker_id)
                    if job:
                        processed += 1
                        self.stdout.write(f'工作 {job.id} ({job.kind}) -> {job.state}')
                        continue
                    if options['once'] or not options['watch']:
                        break
                except (OperationalError, OSError, TimeoutError) as exc:
                    self.stderr.write(str(exc))
                    if not options['watch']:
                        break
                    time.sleep(3)
                if options['watch']:
                    time.sleep(1.5)
        except KeyboardInterrupt:
            self.stdout.write(f'已停止背景工作行程（累計處理 {processed} 筆）')
        finally:
            if stop and stop.exists() and not profile():
                stop.unlink()
