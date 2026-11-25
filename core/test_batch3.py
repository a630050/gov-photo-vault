import hashlib
import io
import json
import os
from datetime import timedelta
from pathlib import Path
import shutil
import tempfile
from PIL import Image
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from .geocoding import clear_boundary_cache, geocode_with_status
from .jobs import process_one_job
from .models import (
    AdminBoundaryVersion,
    Audit,
    Dataset,
    Department,
    Item,
    Membership,
    Photo,
    SavedSearch,
    StorageReconcileReport,
    WorkCollection,
    WorkCollectionItem,
)


def _jpeg_bytes(color=(30, 120, 150)):
    im = Image.new('RGB', (60, 40), color)
    buf = io.BytesIO()
    im.save(buf, 'JPEG')
    return buf.getvalue()


class Batch3Tests(TestCase):
    def setUp(self):
        clear_boundary_cache()
        self.dep = Department.objects.create(name='水利科')
        self.other = Department.objects.create(name='政風室')
        self.member = User.objects.create_user('m1', password='p')
        self.peer = User.objects.create_user('m2', password='p')
        self.viewer = User.objects.create_user('v1', password='p')
        self.out = User.objects.create_user('o1', password='p')
        self.admin = User.objects.create_superuser('adm', password='p')

        Membership.objects.create(
            user=self.member, department=self.dep, role='member',
            permissions={'upload': True, 'organize': True, 'download': True, 'export': True},
        )
        Membership.objects.create(
            user=self.peer, department=self.dep, role='member',
            permissions={'upload': True, 'organize': True, 'download': True},
        )
        Membership.objects.create(
            user=self.viewer, department=self.dep, role='viewer',
            permissions={'upload': False, 'organize': False, 'download': False},
        )
        Membership.objects.create(user=self.out, department=self.other, role='member')

        self.item = Item.objects.create(
            department=self.dep, name='巡查表',
            fields=[{'key': 'note', 'label': '說明', 'type': 'text'}],
        )
        self.ds = Dataset.objects.create(item=self.item, name='淡水河', creator=self.member)
        self.other_item = Item.objects.create(department=self.other, name='稽查', fields=[])
        self.other_ds = Dataset.objects.create(item=self.other_item, name='專案', creator=self.out)
        self.client.force_login(self.member)

    def tearDown(self):
        clear_boundary_cache()

    def post(self, path, data):
        return self.client.post('/api/' + path, data=json.dumps(data), content_type='application/json')

    def test_boundary_statuses_holes_islands_and_version_recompute(self):
        # 1. Status distinction: no_coords, out_of_bounds, boundary_unavailable, ok
        self.assertEqual(geocode_with_status(None, None)['status'], 'no_coords')
        self.assertEqual(geocode_with_status(35.67, 139.65)['status'], 'out_of_bounds')
        self.assertEqual(geocode_with_status(25.033, 121.565, boundary_path='__missing__.json')['status'], 'boundary_unavailable')

        # 2. Polygon with interior hole + island (MultiPolygon) + exact edge point
        geojson = {
            'type': 'FeatureCollection',
            'features': [
                {
                    'type': 'Feature',
                    'properties': {'COUNTYNAME': '新北市', 'TOWNNAME': '測試區', 'VILLAGENAM': '本島里'},
                    'geometry': {
                        'type': 'MultiPolygon',
                        'coordinates': [
                            # Outer square with an excluded interior hole
                            [
                                [[121.0, 25.0], [121.4, 25.0], [121.4, 25.4], [121.0, 25.4], [121.0, 25.0]],
                                [[121.1, 25.1], [121.3, 25.1], [121.3, 25.3], [121.1, 25.3], [121.1, 25.1]],
                            ],
                            # Separate outer island polygon
                            [
                                [[121.6, 25.1], [121.7, 25.1], [121.7, 25.2], [121.6, 25.2], [121.6, 25.1]],
                            ],
                        ],
                    },
                }
            ],
        }
        tmp_dir = Path(settings.BASE_DIR) / '.runtime' / 'test_boundaries'
        tmp_dir.mkdir(parents=True, exist_ok=True)
        bfile = tmp_dir / 'test_holes_islands.geojson'
        try:
            bfile.write_text(json.dumps(geojson), encoding='utf-8')
            sha = hashlib.sha256(bfile.read_bytes()).hexdigest()

            # Inside outer ring outside hole -> ok
            self.assertEqual(geocode_with_status(25.05, 121.05, boundary_path=str(bfile), expected_sha256=sha)['status'], 'ok')
            # Exact boundary edge -> ok
            self.assertEqual(geocode_with_status(25.0, 121.2, boundary_path=str(bfile), expected_sha256=sha)['status'], 'ok')
            # Inside interior hole -> not_found
            self.assertEqual(geocode_with_status(25.2, 121.2, boundary_path=str(bfile), expected_sha256=sha)['status'], 'not_found')
            # On outer island -> ok
            self.assertEqual(geocode_with_status(25.15, 121.65, boundary_path=str(bfile), expected_sha256=sha)['village'], '本島里')
            # Checksum mismatch -> boundary_unavailable
            self.assertEqual(geocode_with_status(25.05, 121.05, boundary_path=str(bfile), expected_sha256='0' * 64)['status'], 'boundary_unavailable')

            # Register & activate via admin API, preserving photo EXIF/coords/attrs during recompute
            p = Photo.objects.create(
                dataset=self.ds, uploader=self.member, name='g.jpg', sha='g1', status='submitted',
                lat=25.15, lon=121.65, attrs={'note': '保留人工欄位'}, extracted={'raw_lat': 25.15},
                form_snapshot=self.item.fields,
            )
            self.client.force_login(self.admin)
            rel_path = os.path.relpath(bfile, settings.BASE_DIR)
            save_res = self.post('boundary/save', {
                'version': 'test-hole-v1',
                'source': 'Unit Test Boundary (CC0)',
                'published_date': '2026-11-25',
                'file_path': rel_path,
                'activate': True,
            })
            self.assertEqual(save_res.status_code, 200, save_res.json())

            job_res = self.post('jobs/create', {'kind': 'region_recompute', 'dataset': self.ds.id, 'boundary_version': 'test-hole-v1'})
            self.assertEqual(job_res.status_code, 200)
            process_one_job('test-worker')

            p.refresh_from_db()
            self.assertEqual(p.admin_county, '新北市')
            self.assertEqual(p.admin_village, '本島里')
            self.assertEqual(p.admin_status, 'ok')
            self.assertEqual(p.admin_version, 'test-hole-v1')
            # Must never alter original coordinates, extracted snapshot, or form attrs
            self.assertAlmostEqual(p.lat, 25.15)
            self.assertEqual(p.extracted, {'raw_lat': 25.15})
            self.assertEqual(p.attrs, {'note': '保留人工欄位'})
        finally:
            if bfile.exists():
                bfile.unlink()
            try:
                tmp_dir.rmdir()
            except OSError:
                pass

    def test_saved_search_validates_invalid_dataset_or_fields_without_widening(self):
        rules = [{'key': 'note', 'type': 'text', 'op': 'contains', 'value': '堤防'}]
        save = self.post('saved_searches/save', {
            'name': '堤防搜尋',
            'display_mode': 'wall',
            'criteria': {'datasets': [self.ds.id], 'fields': rules, 'status': 'submitted'},
        })
        self.assertEqual(save.status_code, 200)
        ss_id = save.json()['saved_search']['id']

        # Apply succeeds initially
        ok_apply = self.post('saved_searches/apply', {'id': ss_id})
        self.assertEqual(ok_apply.status_code, 200)
        self.assertTrue(ok_apply.json()['valid'])

        # Remove user's membership to department -> saved search MUST report invalid and NOT silently widen scope
        Membership.objects.filter(user=self.member).delete()
        bad_apply = self.post('saved_searches/apply', {'id': ss_id})
        self.assertEqual(bad_apply.status_code, 409)
        self.assertFalse(bad_apply.json()['valid'])
        self.assertTrue(bad_apply.json()['warnings'])

    def test_work_collections_permissions_recycled_and_audits(self):
        p1 = Photo.objects.create(dataset=self.ds, uploader=self.member, name='p1.jpg', sha='p1', status='submitted')
        p2 = Photo.objects.create(dataset=self.ds, uploader=self.member, name='p2.jpg', sha='p2', status='submitted')
        p_sec = Photo.objects.create(dataset=self.other_ds, uploader=self.out, name='sec.jpg', sha='ps', status='submitted')

        # Personal collection
        personal = self.post('collections/save', {'name': '我的交接清單', 'description': '待整理'}).json()['collection']
        # Shared collection in self.dep
        shared = self.post('collections/save', {'name': '水利科共用複查', 'department': self.dep.id}).json()['collection']

        # Viewer lacks organize permission -> cannot create shared collection
        self.client.force_login(self.viewer)
        self.assertEqual(self.post('collections/save', {'name': '違規', 'department': self.dep.id}).status_code, 403)
        self.assertEqual(self.post('collections/add_items', {'collection': shared['id'], 'photos': [p1.id]}).status_code, 403)

        # Member adds p1, p2 to shared collection; trying to add p_sec (other dept) is rejected
        self.client.force_login(self.member)
        self.assertEqual(self.post('collections/add_items', {'collection': shared['id'], 'photos': [p1.id, p2.id]}).status_code, 200)
        self.assertEqual(self.post('collections/add_items', {'collection': shared['id'], 'photos': [p_sec.id]}).status_code, 409)

        # Peer in same department sees shared collection but NOT personal collection
        self.client.force_login(self.peer)
        cols = self.post('collections/list', {}).json()['collections']
        self.assertEqual([c['id'] for c in cols], [shared['id']])
        items = self.post('collections/items', {'collection': shared['id']}).json()
        self.assertEqual(items['total'], 2)

        # Viewer sees shared collection items, but batch_tags re-verifies organize permission (403)
        self.client.force_login(self.viewer)
        v_items = self.post('collections/items', {'collection': shared['id']}).json()
        self.assertEqual(v_items['total'], 2)
        self.assertEqual(self.post('batch_tags', {'selection_token': v_items['selection_token'], 'tags': ['未授權']}).status_code, 403)

        # Recycle p2 -> immediately disappears from collection queries!
        Photo.objects.filter(id=p2.id).update(status='recycled')
        self.assertEqual(self.post('collections/items', {'collection': shared['id']}).json()['total'], 1)

        # Revoke viewer's membership -> viewer can no longer access collection or photos
        Membership.objects.filter(user=self.viewer).delete()
        self.assertEqual(self.post('collections/items', {'collection': shared['id']}).status_code, 404)

        # Verify collection audit records exist
        self.assertTrue(Audit.objects.filter(target=shared['id'], action='collection_save').exists())
        self.assertTrue(Audit.objects.filter(target=shared['id'], action='collection_add').exists())

    def test_reconcile_non_destructive_grace_window_and_repair(self):
        tmp_media = Path(tempfile.mkdtemp(prefix='reconcile_media_'))
        try:
            with override_settings(MEDIA_ROOT=tmp_media):
                raw = _jpeg_bytes()
                sha = hashlib.sha256(raw).hexdigest()

                p_ok = Photo.objects.create(dataset=self.ds, uploader=self.member, name='ok.jpg', sha=sha, status='submitted')
                p_ok.file.save('ok.jpg', ContentFile(raw), save=False)
                p_ok.preview.save('ok_prev.jpg', ContentFile(raw), save=True)

                # Photo missing preview on disk (original valid)
                p_no_prev = Photo.objects.create(dataset=self.ds, uploader=self.member, name='noprev.jpg', sha=sha, status='submitted')
                p_no_prev.file.save('noprev.jpg', ContentFile(raw), save=True)

                # Photo with hash mismatch
                p_bad_hash = Photo.objects.create(dataset=self.ds, uploader=self.member, name='badsha.jpg', sha='f' * 64, status='submitted')
                p_bad_hash.file.save('badsha.jpg', ContentFile(raw), save=False)
                p_bad_hash.preview.save('badsha_prev.jpg', ContentFile(raw), save=True)

                # Create an expired orphan file AND a fresh in-grace orphan file
                orphan_old = tmp_media / 'originals' / 'old_orphan.jpg'
                orphan_old.parent.mkdir(parents=True, exist_ok=True)
                orphan_old.write_bytes(b'old-orphan')
                old_ts = (timezone.now() - timedelta(hours=48)).timestamp()
                os.utime(orphan_old, (old_ts, old_ts))

                orphan_fresh = tmp_media / 'originals' / 'uploading_now.jpg'
                orphan_fresh.write_bytes(b'fresh-upload')

                # Non-admin denied
                self.assertEqual(self.post('reconcile/capacity', {}).status_code, 403)
                self.assertEqual(self.post('reconcile/run', {}).status_code, 403)

                # Admin runs non-destructive reconciliation with 24h grace period
                self.client.force_login(self.admin)
                rep = self.post('reconcile/run', {'grace_hours': 24, 'verify_hashes': True})
                self.assertEqual(rep.status_code, 200)
                counts = rep.json()['report']['summary']['counts']
                self.assertEqual(counts['missing_preview'], 1)
                self.assertEqual(counts['hash_mismatch'], 1)
                self.assertEqual(counts['orphan_file'], 1)  # Only old_orphan, fresh file excluded!
                self.assertTrue(orphan_old.exists())
                self.assertTrue(orphan_fresh.exists())

                # Repair missing preview safely regenerates preview without touching orphans
                fix = self.post('reconcile/repair_previews', {})
                self.assertEqual(fix.status_code, 200)
                self.assertEqual(fix.json()['repaired'], 1)
                p_no_prev.refresh_from_db()
                self.assertTrue(p_no_prev.preview)
                self.assertTrue(orphan_old.exists())

                # Confirm orphan cleanup refuses fresh file & referenced file, deletes only expired orphan
                clean = self.post('reconcile/confirm_orphan_cleanup', {
                    'paths': ['originals/old_orphan.jpg', 'originals/uploading_now.jpg', p_ok.file.name],
                    'reason': '人工比對確認為測試殘留檔',
                    'grace_hours': 24,
                })
                self.assertEqual(clean.status_code, 200)
                self.assertEqual(clean.json()['deleted'], ['originals/old_orphan.jpg'])
                self.assertFalse(orphan_old.exists())
                self.assertTrue(orphan_fresh.exists())
                self.assertTrue((tmp_media / p_ok.file.name).exists())

                # Capacity stats endpoint returns dataset counts & bytes
                cap = self.post('reconcile/capacity', {}).json()
                self.assertGreater(cap['datasets'][0]['original_bytes'], 0)
        finally:
            shutil.rmtree(tmp_media, ignore_errors=True)
