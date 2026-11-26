"""Run Django regression tests with an in-memory database and disposable media."""
import argparse
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / '.vendor'))
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('labels', nargs='*')
    parser.add_argument('--gateway', action='store_true')
    parser.add_argument('--fast', action='store_true', help='Use a fast fixture password hasher, never production credentials')
    args = parser.parse_args()
    os.environ.pop('PHOTO_OPS_CONFIG', None)
    os.environ['DJANGO_SETTINGS_MODULE'] = 'gateway_settings' if args.gateway else 'settings'
    from django.conf import settings
    with tempfile.TemporaryDirectory(prefix='photo-isolated-tests-') as directory:
        settings.MEDIA_ROOT = Path(directory)
        settings.DATABASES['default']['NAME'] = ':memory:'
        settings.DATABASES['default']['TEST'] = {'NAME': ':memory:'}
        if args.fast:
            settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
        import django
        django.setup()
        from django.test.utils import get_runner
        labels = args.labels or (['external_gateway.gateway_checks'] if args.gateway else ['core'])
        failures = get_runner(settings)(verbosity=1, interactive=False).run_tests(labels)
        return int(bool(failures))


if __name__ == '__main__':
    sys.exit(main())
