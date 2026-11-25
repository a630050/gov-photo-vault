from datetime import timedelta
import io
import json
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch
import zipfile
from PIL import Image
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from .jobs import LEASE_SECONDS, claim_next_job, process_one_job, run_job
from .models import (
    BackgroundJob,
    Dataset,
    Department,
    Export,
    Item,
    Membership,
    Notice,
    Photo,
    PhotoEmbedding,
    PhotoOcr,
    Request,
    SimilarityPair,
    SimilarityRun,
    Star,
)


def _jpeg_bytes(color=(40, 110, 140)):
    im = Image.new('RGB', (40, 30), color)
    buf = io.BytesIO()
    im.save(buf, 'JPEG')
    return buf.getvalue()


class Batch2Tests(TestCase):
    def setUp(self):
        self.dep = Department.objects.create(name='水利科')
        self.other = Department.objects.create(name='秘書室')
        self.member = User.objects.create_user('m1', password='p')
        self.boss = User.objects.create_user('s1', password='p')
        self.out = User.objects.create_user('o1', password='p')
        self.admin = User.objects.create_superuser('adm', password='p')
        Membership.objects.create(
            user=self.member,
            department=self.dep,
            role='member',
            permissions={'upload': True, 'organize': True, 'download': True, 'export': True, 'analyze': True},
        )
        Membership.objects.create(
            user=self.boss,
            department=self.dep,
            role='supervisor',
            permissions={'upload': True, 'organize': True, 'download': True, 'export': True, 'analyze': True, 'forms': True},
        )
        Membership.objects.create(user=self.out, department=self.other, role='member')
        self.item = Item.objects.create(department=self.dep, name='防汛工程', fields=[{'key': 'note', 'label': '說明', 'type': 'text'}])
        self.ds = Dataset.objects.create(item=self.item, name='115年堤防', creator=self.member)
        self.other_item = Item.objects.create(department=self.other, name='辦公廳舍', fields=[])
        self.other_ds = Dataset.objects.create(item=self.other_item, name='機密資料', creator=self.out)
        self.client.force_login(self.member)

    def post(self, path, data):
        return self.client.post('/api/' + path, data=json.dumps(data), content_type='application/json')

    def test_lightweight_state_and_paginated_inbox(self):
        p = Photo.objects.create(
            dataset=self.ds, uploader=self.member, name='a.jpg', sha='a1', status='submitted',
            tags=['河道'], admin_county='臺北市', admin_town='信義區', admin_village='西村里',
        )
        Photo.objects.create(
            dataset=self.other_ds, uploader=self.out, name='secret.jpg', sha='sec1', status='submitted',
            tags=['機密標籤'], admin_county='高雄市', admin_town='前鎮區',
        )
        for i in range(5):
            Request.objects.create(photo=p, author=self.member, kind='correct', reason=f'r{i}', payload={}, base_version=1)
            Notice.objects.create(user=self.member, text=f'n{i}', read=(i % 2 == 0))

        state = self.client.get('/api/state').json()
        self.assertNotIn('photos', state)
        self.assertNotIn('requests', state)
        self.assertNotIn('notices', state)
        self.assertEqual(state['summary']['visible_photos'], 1)
        self.assertEqual(state['summary']['dataset_counts'][str(self.ds.id)], 1)
        self.assertEqual(state['summary']['unread_notices'], 2)
        self.assertEqual(state['filter_options']['tags'], ['河道'])
        self.assertNotIn('機密標籤', state['filter_options']['tags'])
        self.assertEqual(state['filter_options']['counties'], ['臺北市'])

        # Paginated requests & notices
        r1 = self.post('requests', {'view': 'mine', 'limit': 2, 'offset': 0}).json()
        r2 = self.post('requests', {'view': 'mine', 'limit': 2, 'offset': 2}).json()
        self.assertEqual(r1['total'], 5)
        self.assertTrue(r1['has_next'])
        self.assertEqual(r1['next_offset'], 2)
        self.assertEqual(len(r1['requests']), 2)
        self.assertEqual(len(r2['requests']), 2)
        self.assertTrue({x['id'] for x in r1['requests']}.isdisjoint({x['id'] for x in r2['requests']}))

        n1 = self.post('notices', {'limit': 3, 'offset': 0}).json()
        self.assertEqual(n1['total'], 5)
        self.assertEqual(n1['unread_count'], 2)
        self.assertTrue(n1['has_next'])

    def test_search_pagination_stability_and_full_stats(self):
        base_time = timezone.now()
        created_ids = []
        for i in range(15):
            has_loc = i < 10
            has_time = i < 12
            p = Photo.objects.create(
                dataset=self.ds,
                uploader=self.member,
                name=f'p{i:02d}.jpg',
                sha=f'sha{i}',
                status='submitted',
                captured=(base_time - timedelta(minutes=i)) if has_time else None,
                lat=(25.03 + i * 0.001) if has_loc else None,
                lon=(121.56 + i * 0.001) if has_loc else None,
                admin_county='臺北市' if has_loc else '',
                admin_town='信義區' if has_loc else '',
                tags=['批量'] if i < 13 else ['其他'],
                burst_group='bg-full' if i in (0, 1, 14) else '',
                burst_cover=(i == 0),
            )
            created_ids.append(p.id)

        # Page size limit enforced
        self.assertEqual(self.post('search', {'limit': 201}).status_code, 422)

        # Paginate across tags filter (13 matches across 3 pages of 5)
        seen = []
        offset = 0
        while True:
            res = self.post('search', {'tags': ['批量'], 'limit': 5, 'offset': offset}).json()
            # Full-set stats must reflect ALL 13 matched photos even on page 1
            self.assertEqual(res['total'], 13)
            self.assertEqual(res['located'], 10)
            self.assertEqual(res['missing_location'], 3)
            self.assertEqual(res['region_stats'], [{'county': '臺北市', 'town': '信義區', 'count': 10}])
            self.assertEqual(res['burst_matched_counts'], {'bg-full': 2})
            seen.extend(res['ids'])
            if not res['has_next']:
                break
            offset = res['next_offset']

        self.assertEqual(len(seen), 13)
        self.assertEqual(len(set(seen)), 13)
        # Missing time photos sort after timed photos
        self.assertEqual(seen[-1], created_ids[12])

    def test_backend_map_clusters_match_cluster_search_and_permissions(self):
        p1 = Photo.objects.create(dataset=self.ds, uploader=self.member, name='c1.jpg', sha='c1', status='submitted', lat=25.0330, lon=121.5650)
        p2 = Photo.objects.create(dataset=self.ds, uploader=self.member, name='c2.jpg', sha='c2', status='submitted', lat=25.0331, lon=121.5651)
        p_far = Photo.objects.create(dataset=self.ds, uploader=self.member, name='far.jpg', sha='c3', status='submitted', lat=22.6273, lon=120.3014)
        # Unauthorized & recycled photos must never enter cluster counts
        Photo.objects.create(dataset=self.other_ds, uploader=self.out, name='sec.jpg', sha='c4', status='submitted', lat=25.0330, lon=121.5650)
        Photo.objects.create(dataset=self.ds, uploader=self.member, name='rec.jpg', sha='c5', status='recycled', lat=25.0330, lon=121.5650)

        res = self.post('map_clusters', {
            'bounds': {'north': 25.5, 'south': 22.0, 'east': 122.0, 'west': 120.0},
            'zoom': 11,
            'grid_px': 55,
        }).json()
        self.assertEqual(res['total_located'], 3)
        clusters = sorted(res['clusters'], key=lambda c: -c['count'])
        self.assertEqual([c['count'] for c in clusters], [2, 1])
        self.assertEqual(clusters[1]['single_photo_id'], p_far.id)

        # Clicking the 2-photo cluster executes backend search with identical cluster spec
        clicked = self.post('search', {
            'cluster': {'zoom': clusters[0]['zoom'], 'bx': clusters[0]['bx'], 'by': clusters[0]['by'], 'grid_px': clusters[0]['grid_px']},
        }).json()
        self.assertEqual(clicked['total'], 2)
        self.assertEqual(set(clicked['ids']), {p1.id, p2.id})

    def test_cross_page_selection_token_permissions_and_limits(self):
        photos = [
            Photo.objects.create(dataset=self.ds, uploader=self.member, name=f'x{i}.jpg', sha=f'x{i}', status='submitted')
            for i in range(6)
        ]
        # Page 1 returns 2 items, but selection_token represents all 6
        s = self.post('search', {'limit': 2, 'offset': 0}).json()
        self.assertEqual(len(s['photos']), 2)
        self.assertEqual(s['total'], 6)
        token = s['selection_token']

        # Exclude 1 item across pages -> 5 starred
        star_res = self.post('batch_star', {'selection_token': token, 'excluded_photos': [photos[0].id]})
        self.assertEqual(star_res.status_code, 200)
        self.assertEqual(star_res.json()['count'], 5)
        self.assertEqual(Star.objects.filter(user=self.member).count(), 5)

        # Batch tags on token
        tag_res = self.post('batch_tags', {'selection_token': token, 'excluded_photos': [photos[0].id], 'tags': ['跨頁標籤']})
        self.assertEqual(tag_res.status_code, 200)
        self.assertEqual(tag_res.json()['count'], 5)

        # Revoke organize permission -> cross-page batch_tags immediately rejected
        m = Membership.objects.get(user=self.member)
        m.permissions['organize'] = False
        m.save()
        self.assertEqual(
            self.post('batch_tags', {'selection_token': token, 'tags': ['越權']}).status_code,
            403,
        )

        # Cross-user token rejected
        self.client.force_login(self.boss)
        self.assertEqual(self.post('batch_star', {'selection_token': token}).status_code, 403)

    def test_background_export_job_reauth_and_download(self):
        f1 = SimpleUploadedFile('e1.jpg', _jpeg_bytes((40, 110, 140)), content_type='image/jpeg')
        f2 = SimpleUploadedFile('e2.jpg', _jpeg_bytes((80, 140, 180)), content_type='image/jpeg')
        up1 = self.client.post('/api/upload', {'dataset': self.ds.id, 'files': f1}).json()['results'][0]['id']
        up2 = self.client.post('/api/upload', {'dataset': self.ds.id, 'files': f2}).json()['results'][0]['id']
        for pid, lat, lon in ((up1, 25.033, 121.565), (up2, 25.040, 121.570)):
            p = Photo.objects.get(id=pid)
            self.post('submit', {'photo': pid, 'version': p.version, 'lat': lat, 'lon': lon, 'attrs': {}})

        search = self.post('search', {'limit': 1}).json()
        job_res = self.post('jobs/create', {'kind': 'export', 'selection_token': search['selection_token']})
        self.assertEqual(job_res.status_code, 200)
        job_id = job_res.json()['job']['id']

        call_command('job_worker', once=True)
        detail = self.post('jobs/detail', {'job': job_id}).json()['job']
        self.assertEqual(detail['state'], 'completed')
        self.assertEqual(detail['completed_count'], 2)
        dl_url = detail['result_data']['url']

        # Download succeeds with valid permissions
        dl = self.client.get(dl_url)
        self.assertEqual(dl.status_code, 200)
        zip_bytes = b''.join(dl.streaming_content)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            self.assertIn('metadata.csv', names)
            csv_text = zf.read('metadata.csv').decode('utf-8-sig')
            self.assertIn('臺北市', csv_text)

        # Revoking download permission blocks subsequent download
        m = Membership.objects.get(user=self.member)
        m.permissions['download'] = False
        m.save()
        self.assertEqual(self.client.get(dl_url).status_code, 403)

    def test_job_worker_lease_takeover_retry_idempotency_and_cancel(self):
        pids = []
        for idx in range(3):
            raw = _jpeg_bytes((100 + idx * 10, 120, 120))
            up = self.client.post('/api/upload', {'dataset': self.ds.id, 'files': SimpleUploadedFile(f's{idx}.jpg', raw, content_type='image/jpeg')}).json()['results'][0]['id']
            p = Photo.objects.get(id=up)
            self.post('submit', {'photo': up, 'version': p.version, 'attrs': {}})
            pids.append(up)

        # Enqueue similarity job and simulate crash mid-run (stale lease)
        created = self.post('jobs/create', {'kind': 'similarity', 'dataset': self.ds.id, 'hours': 24}).json()['job']
        job = BackgroundJob.objects.get(id=created['id'])
        run = SimilarityRun.objects.get(job=job)

        # Simulate crashed worker holding expired lease after 1 fingerprint already saved
        from .similarity import fingerprint
        p0 = Photo.objects.get(id=pids[0])
        run.fingerprints = [{'id': p0.id, **fingerprint(p0.file)}]
        run.state = 'running'
        run.save()
        # Simulate duplicate recovery if an earlier partial run created a pair before crash
        SimilarityPair.objects.create(run=run, a_id=pids[0], b_id=pids[1], distances={'dhash': 0, 'ahash': 0}, time_state='needs_time')
        job.state = 'running'
        job.worker_id = 'dead-worker'
        job.lease_until = timezone.now() - timedelta(seconds=LEASE_SECONDS + 10)
        job.save()

        # New worker steals expired lease and completes without duplicating existing SimilarityPair
        finished = process_one_job(worker_id='recovery-worker')
        self.assertIsNotNone(finished)
        self.assertEqual(finished.state, 'completed')
        self.assertEqual(finished.worker_id, 'recovery-worker')
        run.refresh_from_db()
        self.assertEqual(run.state, 'completed')
        self.assertEqual(run.pairs.count(), 3)

        # Cancel pending job
        j2 = self.post('jobs/create', {'kind': 'similarity', 'dataset': self.ds.id, 'hours': 24}).json()['job']
        c_res = self.post('jobs/cancel', {'job': j2['id']})
        self.assertEqual(c_res.status_code, 200)
        self.assertEqual(c_res.json()['job']['state'], 'cancelled')

        # Retry cancelled job
        r_res = self.post('jobs/retry', {'job': j2['id']})
        self.assertEqual(r_res.status_code, 200)
        self.assertEqual(r_res.json()['job']['state'], 'pending')

    def test_region_recompute_job_and_stale_coordinate_guard(self):
        p = Photo.objects.create(
            dataset=self.ds, uploader=self.member, name='geo.jpg', sha='geo1',
            status='submitted', lat=25.033, lon=121.565,
        )
        self.client.force_login(self.boss)
        res = self.post('jobs/create', {'kind': 'region_recompute', 'dataset': self.ds.id, 'only_failed': True})
        self.assertEqual(res.status_code, 200)
        job_id = res.json()['job']['id']

        # Simulate coordinates changing concurrently right as geocode_with_status computes the old coordinate
        from .geocoding import geocode_with_status as real_geocode
        def mutating_geocode(lat, lon, **kwargs):
            result = real_geocode(lat, lon, **kwargs)
            Photo.objects.filter(id=p.id).update(lat=22.6273, lon=120.3014, admin_county='高雄市', admin_town='前金區', admin_village='')
            return result

        with patch('core.jobs.geocode_with_status', side_effect=mutating_geocode):
            process_one_job(worker_id='geo-worker')

        p.refresh_from_db()
        # Stale worker result ('臺北市') MUST NOT overwrite newer '高雄市' coordinates/region
        self.assertAlmostEqual(p.lat, 22.6273)
        self.assertEqual(p.admin_county, '高雄市')
        detail = self.post('jobs/detail', {'job': job_id}).json()['job']
        self.assertEqual(detail['state'], 'completed')

    def test_f04_cluster_respects_viewport_bounds_exact_repro(self):
        p1 = Photo.objects.create(dataset=self.ds, uploader=self.member, name='c1.jpg', sha='c1', status='submitted', lat=25.0, lon=121.5)
        p2 = Photo.objects.create(dataset=self.ds, uploader=self.member, name='c2.jpg', sha='c2', status='submitted', lat=25.0, lon=121.50001)
        p3 = Photo.objects.create(dataset=self.ds, uploader=self.member, name='c3.jpg', sha='c3', status='submitted', lat=25.0, lon=121.50002)

        bounds = {'north': 25.2, 'south': 24.8, 'east': 121.500015, 'west': 121.3}
        cl_res = self.post('map_clusters', {'bounds': bounds, 'zoom': 11, 'grid_px': 55})
        self.assertEqual(cl_res.status_code, 200)
        clusters = cl_res.json()['clusters']
        self.assertEqual(len(clusters), 1)
        c = clusters[0]
        self.assertEqual(c['count'], 2)
        self.assertTrue(c.get('cluster_token'))

        # Querying search with cluster_token (as frontend click does) MUST return exactly 2 photos, not 3
        s_res = self.post('search', {
            'cluster': {'zoom': c['zoom'], 'bx': c['bx'], 'by': c['by'], 'grid_px': c['grid_px']},
            'bounds': c['bounds'],
            'cluster_token': c['cluster_token'],
            'limit': 100,
        })
        self.assertEqual(s_res.status_code, 200)
        body = s_res.json()
        self.assertEqual(body['total'], 2)
        self.assertEqual({x['id'] for x in body['photos']}, {p1.id, p2.id})

        # Even if client omits bounds when passing cluster_token, backend enforces token's exact bounds
        s_tok = self.post('search', {
            'cluster': {'zoom': c['zoom'], 'bx': c['bx'], 'by': c['by'], 'grid_px': c['grid_px']},
            'cluster_token': c['cluster_token'],
            'limit': 100,
        })
        self.assertEqual(s_tok.status_code, 200)
        self.assertEqual(s_tok.json()['total'], 2)
        self.assertNotIn(p3.id, {x['id'] for x in s_tok.json()['photos']})

    def test_f02_historical_fields_zero_false_multi_missing(self):
        it = Item.objects.create(
            department=self.dep,
            name='工程檢核',
            fields=[
                {'key': 'cnt', 'label': '數量', 'type': 'integer'},
                {'key': 'ok', 'label': '合格', 'type': 'boolean'},
                {'key': 'tags_m', 'label': '項目', 'type': 'multi', 'options': ['A', 'B']},
                {'key': 'legacy_sel', 'label': '舊分類', 'type': 'select', 'options': ['舊甲', '舊乙']},
            ],
        )
        ds = Dataset.objects.create(item=it, name='歷史欄位測試集', creator=self.member)
        p_match = Photo.objects.create(
            dataset=ds, uploader=self.member, name='h1.jpg', sha='h1', status='submitted',
            form_snapshot=it.fields,
            attrs={'cnt': 0, 'ok': False, 'tags_m': ['A'], 'legacy_sel': '舊甲'},
        )
        p_missing = Photo.objects.create(
            dataset=ds, uploader=self.member, name='h2.jpg', sha='h2', status='submitted',
            form_snapshot=it.fields[:3],
            attrs={'cnt': 2, 'ok': True, 'tags_m': ['B']},
        )
        # Remove legacy_sel from current item definition
        it.fields = it.fields[:3]
        it.save()

        opts = self.post('filter_options', {'datasets': [ds.id], 'status': 'submitted'}).json()
        legacy = next((f for f in opts['fields'] if f['key'] == 'legacy_sel'), None)
        self.assertIsNotNone(legacy)
        self.assertTrue(legacy['historical'])
        self.assertEqual(legacy['options'], ['舊甲', '舊乙'])

        # Query 0, false, multi, and missing
        r_zero_false = self.post('search', {
            'datasets': [ds.id],
            'fields': [
                {'key': 'cnt', 'type': 'integer', 'op': 'range', 'min': 0, 'max': 0},
                {'key': 'ok', 'type': 'boolean', 'op': 'equals', 'value': False},
                {'key': 'tags_m', 'type': 'multi', 'op': 'in', 'values': ['A']},
            ],
        }).json()
        self.assertEqual([x['id'] for x in r_zero_false['photos']], [p_match.id])

        r_missing = self.post('search', {
            'datasets': [ds.id],
            'fields': [{'key': 'legacy_sel', 'type': 'select', 'op': 'missing'}],
        }).json()
        self.assertEqual([x['id'] for x in r_missing['photos']], [p_missing.id])

    def test_f09_semantic_index_job_strictly_respects_dataset_scope(self):
        pa = Photo.objects.create(dataset=self.ds, uploader=self.member, name='a.jpg', sha='sa', status='submitted')
        pb = Photo.objects.create(dataset=self.other_ds, uploader=self.out, name='b.jpg', sha='sb', status='submitted')
        vec = [1.0] + [0.0] * 1023

        self.client.force_login(self.admin)
        res = self.post('jobs/create', {'kind': 'semantic_index', 'target': 'all', 'mode': 'build', 'datasets': [self.ds.id]})
        self.assertEqual(res.status_code, 200)
        job_id = res.json()['job']['id']
        self.assertEqual(res.json()['job']['total'], 2)

        with (
            patch('core.jobs.semantic_readiness', return_value=True),
            patch('core.jobs.ocr_readiness', return_value=True),
            patch('core.semantic.image_vectors', side_effect=lambda rows: [{'id': p.id, 'vector': vec} for p in rows]),
            patch('core.ocr.ocr_images', side_effect=lambda rows: [{'id': p.id, 'text': f'ok-{p.id}'} for p in rows]),
        ):
            job = process_one_job('f09-w1')

        self.assertEqual(job.id.hex, job_id.replace('-', ''))
        self.assertEqual(job.state, 'completed')
        self.assertEqual(job.completed_count, 2)
        self.assertLessEqual(job.completed_count, job.total)
        # Dataset A indexed; Dataset B completely untouched!
        self.assertTrue(PhotoEmbedding.objects.filter(photo=pa, state='ready').exists())
        self.assertTrue(PhotoOcr.objects.filter(photo=pa, state='ready').exists())
        self.assertFalse(PhotoEmbedding.objects.filter(photo=pb).exists())
        self.assertFalse(PhotoOcr.objects.filter(photo=pb).exists())

        # Rebuild on dataset A must NOT delete dataset B existing index
        PhotoEmbedding.objects.create(photo=pb, model_key='chinese-clip-rn50-717ba215-t2s-exif-v1', source_sha='sb', vector=vec, state='ready')
        self.post('jobs/create', {'kind': 'semantic_index', 'target': 'semantic', 'mode': 'rebuild', 'datasets': [self.ds.id]})
        with (
            patch('core.jobs.semantic_readiness', return_value=True),
            patch('core.semantic.image_vectors', side_effect=lambda rows: [{'id': p.id, 'vector': vec} for p in rows]),
        ):
            job_rb = process_one_job('f09-w2')
        self.assertEqual(job_rb.state, 'completed')
        self.assertTrue(PhotoEmbedding.objects.filter(photo=pb, state='ready').exists())

    def test_f10_semantic_index_fails_and_preserves_index_when_model_unavailable(self):
        p = Photo.objects.create(dataset=self.ds, uploader=self.member, name='f10.jpg', sha='s10', status='submitted')
        vec = [1.0] + [0.0] * 1023
        PhotoEmbedding.objects.create(
            photo=p, model_key='chinese-clip-rn50-717ba215-t2s-exif-v1', source_sha='s10', vector=vec, state='ready'
        )

        self.client.force_login(self.admin)
        self.post('jobs/create', {'kind': 'semantic_index', 'target': 'semantic', 'mode': 'rebuild', 'datasets': [self.ds.id]})
        with patch('core.jobs.semantic_readiness', return_value=False):
            job = process_one_job('f10-w1')

        self.assertEqual(job.state, 'failed')
        self.assertIn('不可用', job.error)
        # Existing usable index MUST remain intact!
        self.assertTrue(PhotoEmbedding.objects.filter(photo=p, state='ready').exists())

        # Partial failure when target='all' and only OCR is unavailable
        self.post('jobs/create', {'kind': 'semantic_index', 'target': 'all', 'mode': 'rebuild', 'datasets': [self.ds.id]})
        with (
            patch('core.jobs.semantic_readiness', return_value=True),
            patch('core.jobs.ocr_readiness', return_value=False),
            patch('core.semantic.image_vectors', side_effect=lambda rows: [{'id': r.id, 'vector': vec} for r in rows]),
        ):
            job_part = process_one_job('f10-w2')
        self.assertEqual(job_part.state, 'partial_failed')
        self.assertIn('OCR', job_part.error)

    def test_f11_partial_export_retry_rebuilds_complete_zip(self):
        tmp_media = Path(tempfile.mkdtemp(prefix='export_f11_'))
        try:
            with override_settings(MEDIA_ROOT=tmp_media):
                raw1 = _jpeg_bytes((10, 20, 30))
                raw2 = _jpeg_bytes((90, 100, 110))
                p1 = Photo.objects.create(dataset=self.ds, uploader=self.member, name='e1.jpg', sha='e1', status='submitted')
                p1.file.save('e1.jpg', ContentFile(raw1), save=True)
                p2 = Photo.objects.create(dataset=self.ds, uploader=self.member, name='e2.jpg', sha='e2', status='submitted')
                p2.file.save('e2.jpg', ContentFile(raw2), save=True)

                # Temporarily move p2's file away to force partial_failed export
                p2_path = tmp_media / p2.file.name
                backup_bytes = p2_path.read_bytes()
                p2_path.unlink()

                res = self.post('jobs/create', {'kind': 'export', 'photos': [p1.id, p2.id]})
                self.assertEqual(res.status_code, 200)
                job_id = res.json()['job']['id']

                job1 = process_one_job('f11-w1')
                self.assertEqual(job1.state, 'partial_failed')
                self.assertEqual(job1.completed_count, 1)
                self.assertEqual(job1.failed_count, 1)

                # Retry BEFORE restoring p2 -> MUST stay partial_failed (never mark completed with failed_count=0!)
                self.assertEqual(self.post('jobs/retry', {'job': job_id}).status_code, 200)
                job2 = process_one_job('f11-w2')
                self.assertEqual(job2.state, 'partial_failed')
                self.assertEqual(job2.failed_count, 1)

                # Restore p2 original file, retry -> ZIP MUST physically contain both photos and metadata rows
                p2_path.write_bytes(backup_bytes)
                self.assertEqual(self.post('jobs/retry', {'job': job_id}).status_code, 200)
                job3 = process_one_job('f11-w3')
                self.assertEqual(job3.state, 'completed')
                self.assertEqual(job3.completed_count, 2)
                self.assertEqual(job3.failed_count, 0)

                exp = Export.objects.filter(job_id=job3.id).first()
                self.assertIsNotNone(exp)
                self.assertEqual(set(exp.photo_ids), {p1.id, p2.id})
                with zipfile.ZipFile(tmp_media / exp.file.name, 'r') as zf:
                    names = set(zf.namelist())
                    self.assertIn(f'photos/{p1.id}.jpg', names)
                    self.assertIn(f'photos/{p2.id}.jpg', names)
                    csv_text = zf.read('metadata.csv').decode('utf-8-sig')
                    self.assertIn('e1.jpg', csv_text)
                    self.assertIn('e2.jpg', csv_text)
        finally:
            shutil.rmtree(tmp_media, ignore_errors=True)
