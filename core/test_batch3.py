import hashlib
import io
import json
import os
from datetime import timedelta
from pathlib import Path
import shutil
import tempfile
import uuid
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
    BackgroundJob,
    Dataset,
    Department,
    ExternalImportReceipt,
    ExternalUploadGrant,
    FacePhotoIndex,
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

                # Confirm orphan cleanup refuses fresh file & referenced file, deletes only expired orphan bound to report
                report_id = rep.json()['report']['id']
                clean = self.post('reconcile/confirm_orphan_cleanup', {
                    'report_id': report_id,
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

    def test_f05_orphan_cleanup_protects_control_files_links_and_report_binding(self):
        tmp_media = Path(tempfile.mkdtemp(prefix='reconcile_f05_'))
        outside_dir = Path(tempfile.mkdtemp(prefix='reconcile_outside_'))
        try:
            with override_settings(MEDIA_ROOT=tmp_media):
                old_ts = (timezone.now() - timedelta(hours=72)).timestamp()

                storage_id = tmp_media / '.photo-storage-id'
                storage_id.write_text('vault-id-001', encoding='utf-8')
                os.utime(storage_id, (old_ts, old_ts))

                lock_file = tmp_media / 'originals' / 'worker.lock'
                lock_file.parent.mkdir(parents=True, exist_ok=True)
                lock_file.write_text('locked', encoding='utf-8')
                os.utime(lock_file, (old_ts, old_ts))

                valid_orphan = tmp_media / 'originals' / 'valid_orphan.jpg'
                valid_orphan.write_bytes(b'valid-orphan-data')
                os.utime(valid_orphan, (old_ts, old_ts))

                outside_secret = outside_dir / 'keep.txt'
                outside_secret.write_text('do-not-delete', encoding='utf-8')

                self.client.force_login(self.admin)
                rep = self.post('reconcile/run', {'grace_hours': 24, 'verify_hashes': False})
                self.assertEqual(rep.status_code, 200)
                report_id = rep.json()['report']['id']
                reported_paths = [i['path'] for i in rep.json()['report']['issues'] if i['kind'] == 'orphan_file']
                self.assertEqual(reported_paths, ['originals/valid_orphan.jpg'])

                # Create an expired orphan AFTER report so it is not in report
                unreported = tmp_media / 'originals' / 'manual_not_in_report.jpg'
                unreported.write_bytes(b'manual-data')
                os.utime(unreported, (old_ts, old_ts))

                # Missing report_id must be rejected
                no_rep = self.post('reconcile/confirm_orphan_cleanup', {
                    'paths': ['originals/valid_orphan.jpg'],
                    'reason': '缺少報告測試',
                })
                self.assertEqual(no_rep.status_code, 422)
                self.assertTrue(valid_orphan.exists())

                # Protected storage ID file MUST be rejected and preserved
                res_id = self.post('reconcile/confirm_orphan_cleanup', {
                    'report_id': report_id,
                    'paths': ['.photo-storage-id'],
                    'reason': '試圖刪除識別檔',
                })
                self.assertEqual(res_id.status_code, 422)
                self.assertTrue(storage_id.exists())

                # Lock file & traversal MUST be rejected
                res_lock = self.post('reconcile/confirm_orphan_cleanup', {
                    'report_id': report_id,
                    'paths': ['originals/worker.lock'],
                    'reason': '鎖定檔',
                })
                self.assertEqual(res_lock.status_code, 422)
                self.assertTrue(lock_file.exists())

                res_trav = self.post('reconcile/confirm_orphan_cleanup', {
                    'report_id': report_id,
                    'paths': ['../' + outside_dir.name + '/keep.txt'],
                    'reason': '越界路徑',
                })
                self.assertEqual(res_trav.status_code, 422)
                self.assertTrue(outside_secret.exists())

                # Optional symlink test (skip silently if OS lacks symlink privilege)
                link_path = tmp_media / 'originals' / 'escape_link.jpg'
                try:
                    os.symlink(outside_secret, link_path)
                except OSError:
                    link_path = None
                if link_path is not None:
                    res_link = self.post('reconcile/confirm_orphan_cleanup', {
                        'report_id': report_id,
                        'paths': ['originals/escape_link.jpg'],
                        'reason': '連結路徑',
                    })
                    self.assertEqual(res_link.status_code, 422)
                    self.assertTrue(outside_secret.exists())

                # Manual unreported path must NOT be deleted
                res_unrep = self.post('reconcile/confirm_orphan_cleanup', {
                    'report_id': report_id,
                    'paths': ['originals/manual_not_in_report.jpg'],
                    'reason': '未在報告內',
                })
                self.assertEqual(res_unrep.status_code, 200)
                self.assertEqual(res_unrep.json()['deleted'], [])
                self.assertTrue(unreported.exists())

                # Valid orphan referenced after report must NOT be deleted
                Photo.objects.create(
                    dataset=self.ds, uploader=self.member, name='valid_orphan.jpg',
                    sha='v1', status='submitted', file='originals/valid_orphan.jpg',
                )
                res_ref = self.post('reconcile/confirm_orphan_cleanup', {
                    'report_id': report_id,
                    'paths': ['originals/valid_orphan.jpg'],
                    'reason': '報告後已有紀錄引用',
                })
                self.assertEqual(res_ref.status_code, 200)
                self.assertEqual(res_ref.json()['deleted'], [])
                self.assertTrue(valid_orphan.exists())
        finally:
            shutil.rmtree(tmp_media, ignore_errors=True)
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_f06_region_recompute_stops_when_actor_revoked_or_deactivated(self):
        sup = User.objects.create_user('sup_f06', password='p')
        Membership.objects.create(user=sup, department=self.dep, role='supervisor')
        p1 = Photo.objects.create(
            dataset=self.ds, uploader=self.member, name='r1.jpg', sha='r1',
            status='submitted', lat=25.033, lon=121.565,
        )
        p2 = Photo.objects.create(
            dataset=self.ds, uploader=self.member, name='r2.jpg', sha='r2',
            status='submitted', lat=25.034, lon=121.566,
        )

        # Case 1: Supervisor loses department supervisor role before worker starts
        self.client.force_login(sup)
        res_ds = self.post('jobs/create', {'kind': 'region_recompute', 'dataset': self.ds.id})
        self.assertEqual(res_ds.status_code, 200)
        Membership.objects.filter(user=sup, department=self.dep).update(role='member')

        job1 = process_one_job('f06-w1')
        self.assertEqual(job1.state, 'failed')
        self.assertIn('權限', job1.error)
        p1.refresh_from_db()
        self.assertEqual(p1.admin_county, '')

        # Case 2: System-wide recompute creator loses superuser status before worker starts
        adm2 = User.objects.create_superuser('adm_f06', password='p')
        self.client.force_login(adm2)
        res_sys = self.post('jobs/create', {'kind': 'region_recompute'})
        self.assertEqual(res_sys.status_code, 200)
        User.objects.filter(id=adm2.id).update(is_superuser=False)

        job2 = process_one_job('f06-w2')
        self.assertEqual(job2.state, 'failed')
        self.assertIn('系統管理者', job2.error)
        p2.refresh_from_db()
        self.assertEqual(p2.admin_county, '')

        # Case 3: Actor deactivated before worker starts -> aborts cleanly
        Membership.objects.filter(user=sup, department=self.dep).update(role='supervisor')
        self.client.force_login(sup)
        res_deact = self.post('jobs/create', {'kind': 'region_recompute', 'dataset': self.ds.id})
        self.assertEqual(res_deact.status_code, 200)
        User.objects.filter(id=sup.id).update(is_active=False)

        job3 = process_one_job('f06-w3')
        self.assertEqual(job3.state, 'failed')
        self.assertIn('停用', job3.error)
        p1.refresh_from_db()
        self.assertEqual(p1.admin_county, '')

    def test_f07_reconcile_defers_active_in_grace_and_flags_stalled_jobs(self):
        tmp_media = Path(tempfile.mkdtemp(prefix='reconcile_f07_'))
        try:
            with override_settings(MEDIA_ROOT=tmp_media):
                now = timezone.now()
                raw = _jpeg_bytes()
                sha = hashlib.sha256(raw).hexdigest()

                # Photo A: newly submitted, files/index still processing via active jobs within 24h grace
                p_active = Photo.objects.create(dataset=self.ds, uploader=self.member, name='a.jpg', sha=sha, status='submitted')
                # Photo B: older than 24h without active job -> MUST be detected as missing
                p_old = Photo.objects.create(dataset=self.ds, uploader=self.member, name='old.jpg', sha=sha, status='submitted')
                Photo.objects.filter(id=p_old.id).update(created=now - timedelta(hours=48))
                # Photo C: within grace, but covered ONLY by a stalled/expired job -> NOT deferred
                p_stalled = Photo.objects.create(dataset=self.ds, uploader=self.member, name='stalled.jpg', sha=sha, status='submitted')

                BackgroundJob.objects.create(
                    kind='export',
                    actor=self.admin,
                    dataset=self.ds,
                    state='running',
                    lease_until=now + timedelta(minutes=5),
                    scope={'photo_ids': [p_active.id]},
                )
                BackgroundJob.objects.create(
                    kind='semantic_index',
                    actor=self.admin,
                    dataset=self.ds,
                    state='pending',
                    scope={'datasets': [self.ds.id]},
                    params={'target': 'all', 'mode': 'build'},
                )
                stalled = BackgroundJob.objects.create(
                    kind='export',
                    actor=self.admin,
                    dataset=self.ds,
                    state='running',
                    lease_until=now - timedelta(hours=48),
                    scope={'photo_ids': [p_stalled.id]},
                )
                BackgroundJob.objects.filter(id=stalled.id).update(updated=now - timedelta(hours=48))

                self.client.force_login(self.admin)
                rep = self.post('reconcile/run', {'grace_hours': 24, 'verify_hashes': False})
                self.assertEqual(rep.status_code, 200)
                summary = rep.json()['report']['summary']
                counts = summary['counts']
                deferred = summary['deferred_counts']

                # p_old and p_stalled are faults (2 missing originals/previews); p_active is deferred
                self.assertEqual(counts['missing_original'], 2)
                self.assertEqual(counts['missing_preview'], 2)
                self.assertEqual(counts['stalled_job'], 1)
                self.assertEqual(deferred['missing_original'], 1)
                self.assertEqual(deferred['missing_preview'], 1)
                self.assertEqual(counts['semantic_gap'], 1)  # Only p_old (48h old); p_active & p_stalled in-grace covered by valid pending semantic_index job
                self.assertEqual(deferred['semantic_gap'], 2)
        finally:
            shutil.rmtree(tmp_media, ignore_errors=True)

    def test_f08_reconcile_counts_never_double_count_and_match_total(self):
        tmp_media = Path(tempfile.mkdtemp(prefix='reconcile_f08_'))
        try:
            with override_settings(MEDIA_ROOT=tmp_media):
                now = timezone.now()
                raw = _jpeg_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                p = Photo.objects.create(dataset=self.ds, uploader=self.member, name='f8.jpg', sha=sha, status='submitted')
                p.file.save('f8.jpg', ContentFile(raw), save=False)
                p.preview.save('f8_prev.jpg', ContentFile(raw), save=True)

                FacePhotoIndex.objects.create(photo=p, source_sha=sha, model_key='face-v1', state='failed', error='no face')
                grant = ExternalUploadGrant.objects.create(
                    user=self.member, dataset=self.ds, token_sha='tok', form_id=self.item.id,
                    form_version=1, fields=[], expires=now + timedelta(hours=1),
                )
                rec = ExternalImportReceipt.objects.create(
                    id=uuid.uuid4(), grant=grant, sha='ext1', size=10, state='importing',
                )
                ExternalImportReceipt.objects.filter(id=rec.id).update(updated=now - timedelta(hours=48))

                self.client.force_login(self.admin)
                rep = self.post('reconcile/run', {'grace_hours': 24, 'verify_hashes': True})
                self.assertEqual(rep.status_code, 200)
                summary = rep.json()['report']['summary']
                counts = summary['counts']
                self.assertEqual(counts['face_gap'], 1)
                self.assertEqual(counts['external_stale'], 1)
                self.assertEqual(summary['total_issues'], sum(counts.values()))
        finally:
            shutil.rmtree(tmp_media, ignore_errors=True)
