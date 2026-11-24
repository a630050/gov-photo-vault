import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from unittest import TestCase,mock
import zipfile
from contextlib import closing
from django.test import Client,override_settings
from transfer_common.operations import atomic_json,storage_check,backup,verify,gate

class OperationsTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.media=self.root/'photos';self.media.mkdir()
        (self.media/'.photo-storage-id').write_text('test-storage',encoding='ascii')
        (self.media/'original.jpg').write_bytes(b'local-photo-fixture')
        self.db=self.root/'source.sqlite3'
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE sample (value TEXT)');db.execute("INSERT INTO sample VALUES ('preserved')");db.commit()
        self.config={'version':1,'component':'core','media':str(self.media),'database':str(self.db),'run':str(self.root/'run'),'backups':str(self.root/'backups'),'secret_file':str(self.root/'secret'),'storage_id':'test-storage','min_free_mb':0}
        self.profile=self.root/'profile.json';atomic_json(self.profile,self.config)
        self.env=mock.patch.dict(os.environ,{'PHOTO_OPS_CONFIG':str(self.profile)});self.env.start();self.addCleanup(self.env.stop)
    def test_snapshot_verified_restore_preserves_db_and_files(self):
        archive=backup(self.db,self.media,self.root/'backups')
        self.assertEqual(verify(archive)['component'],'core')
        restored=self.root/'restored';verify(archive,restored)
        self.assertEqual((restored/'storage/original.jpg').read_bytes(),b'local-photo-fixture')
        with closing(sqlite3.connect(restored/'data.sqlite3')) as db:self.assertEqual(db.execute('SELECT value FROM sample').fetchone()[0],'preserved')
        with self.assertRaises(ValueError):verify(archive,restored)
        self.assertEqual(json.loads((self.root/'run/last-backup.json').read_text())['verified'],True)
    def test_missing_or_wrong_storage_fails_closed(self):
        storage_check(self.config,write=True)
        (self.media/'.photo-storage-id').write_text('other',encoding='ascii')
        with self.assertRaises(OSError):storage_check(self.config)
        from transfer_common.ops_django import GuardedStorage
        with self.assertRaises(OSError):GuardedStorage(location=self.media).path('new.jpg')
        with self.assertRaises(OSError):backup(self.db,self.media,self.root/'backups')
    def test_unsafe_duplicate_and_corrupt_archives_rejected(self):
        bad=self.root/'bad.zip'
        for name in ('../escaped','/absolute','storage/../../escaped','C:/escaped','storage\\escaped'):
            with zipfile.ZipFile(bad,'w') as z:z.writestr(name,b'x');z.writestr('manifest.json','{}')
            with self.assertRaises(ValueError):verify(bad)
        archive=backup(self.db,self.media,self.root/'backups')
        with zipfile.ZipFile(archive) as z:items={name:z.read(name) for name in z.namelist()}
        items['storage/original.jpg']=b'x'*len(items['storage/original.jpg'])
        with zipfile.ZipFile(bad,'w') as z:
            for name,content in items.items():z.writestr(name,content)
        with self.assertRaisesRegex(ValueError,'checksum'):verify(bad)
    def test_snapshot_waits_for_writers(self):
        with gate():
            def exclusive():
                with self.assertRaises(TimeoutError):
                    with gate(exclusive=True,timeout=.1):pass
            thread=threading.Thread(target=exclusive);thread.start();thread.join(3);self.assertFalse(thread.is_alive())
        with gate(exclusive=True):
            with self.assertRaises(TimeoutError):
                with gate(timeout=.1):pass
    def test_parallel_readers_are_allowed(self):
        result=[]
        def read():
            with gate(timeout=.2):result.append(True)
        with gate():
            thread=threading.Thread(target=read);thread.start();thread.join(2)
        self.assertEqual(result,[True])
    def test_backup_not_inside_storage(self):
        with self.assertRaises(ValueError):backup(self.db,self.media,self.media/'backup')
    def test_missing_referenced_photo_rejected(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE core_photo (file TEXT, preview TEXT)')
            db.execute("INSERT INTO core_photo VALUES ('missing.jpg','')");db.commit()
        with self.assertRaisesRegex(ValueError,'Referenced file missing'):backup(self.db,self.media,self.root/'backups')
    def test_details_reject_non_admin(self):
        from types import SimpleNamespace
        from django.test import RequestFactory
        from transfer_common.ops_django import status
        request=RequestFactory().get('/ops/status');request.user=SimpleNamespace(is_authenticated=True,is_superuser=False)
        self.assertEqual(status(request).status_code,403)
    def test_duplicate_archive_entries_rejected(self):
        import warnings
        archive=self.root/'duplicate.zip'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(archive,'w') as z:z.writestr('manifest.json','{}');z.writestr('manifest.json','{}')
        with self.assertRaises(ValueError):verify(archive)
    @override_settings(ROOT_URLCONF='urls')
    def test_health_is_minimal_and_details_require_admin(self):
        client=Client()
        self.assertEqual(client.get('/health/live').json(),{'status':'alive'})
        self.assertEqual(client.get('/health/ready').json(),{'status':'ready'})
        self.assertEqual(client.get('/ops/status').status_code,401)
        (self.media/'.photo-storage-id').write_text('wrong',encoding='ascii')
        self.assertEqual(client.get('/health/ready').status_code,503)
        self.assertNotIn(str(self.media),client.get('/health/ready').content.decode())
