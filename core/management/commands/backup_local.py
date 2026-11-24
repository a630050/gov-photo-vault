from django.core.management.base import BaseCommand,CommandError
from django.conf import settings
from transfer_common.operations import backup,profile

class Command(BaseCommand):
    help='Verified SQLite + photo snapshot. Stop legacy dev server first; deployment profile coordinates writers.'
    def add_arguments(self,parser):parser.add_argument('--output')
    def handle(self,*args,**options):
        p=profile()
        destination=options['output'] or (p['backups'] if p else settings.BASE_DIR/'.local-backups')
        try:self.stdout.write(str(backup(settings.DATABASES['default']['NAME'],settings.MEDIA_ROOT,destination,'core')))
        except (OSError,ValueError,TimeoutError) as exc:raise CommandError(str(exc))
