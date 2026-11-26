"""Regression cases for the follow-up review of eb9ab3f. Never use live media."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .jobs import create_job, process_one_job, retry_job
from .models import Audit, Dataset, Department, Item, Photo, PhotoEmbedding, PhotoOcr, StorageReconcileReport
from .reconcile import confirm_orphan_cleanup, run_reconciliation, compute_capacity_report
from .semantic_engine import DIMENSIONS, MODEL_KEY
from .ocr_engine import MODEL_KEY as OCR_KEY


class FollowupRegressionTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='photo-followup-')
        self.addCleanup(self.temp.cleanup)
        self.media = Path(self.temp.name).resolve()
        self.settings_override = override_settings(MEDIA_ROOT=self.media)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.admin = User.objects.create_superuser('review-admin', password='test')
        dept = Department.objects.create(name='測試科')
        item = Item.objects.create(department=dept, name='測試表單')
        self.dataset = Dataset.objects.create(item=item, name='測試資料集', creator=self.admin)

    def orphan(self):
        path = self.media / 'old.jpg'
        path.write_bytes(b'old!')
        stamp = timezone.now().timestamp() - 72 * 3600
        os.utime(path, (stamp, stamp))
        report = run_reconciliation(self.admin, verify_hashes=False)
        return path, report

    def cleanup(self, report):
        return confirm_orphan_cleanup(self.admin, ['old.jpg'], '測試清理', report_id=report.id)

    def test_report_bound_unchanged_orphan_is_deleted(self):
        path, report = self.orphan()
        self.assertEqual(self.cleanup(report)['deleted'], ['old.jpg'])
        self.assertFalse(path.exists())
        self.assertFalse((self.media / '.cleanup-hold').exists())

    def test_replaced_same_size_old_file_is_preserved(self):
        path, report = self.orphan()
        path.unlink()
        path.write_bytes(b'new!')
        stamp = timezone.now().timestamp() - 48 * 3600
        os.utime(path, (stamp, stamp))
        self.assertEqual(self.cleanup(report)['deleted'], [])
        self.assertEqual(path.read_bytes(), b'new!')

    def test_content_change_with_preserved_timestamp_is_detected(self):
        path, report = self.orphan()
        stamp = path.stat()
        path.write_bytes(b'new!')
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(self.cleanup(report)['deleted'], [])
        self.assertEqual(path.read_bytes(), b'new!')

    def test_reference_added_during_cleanup_prevents_deletion(self):
        path, report = self.orphan()
        original = Path.is_file
        inserted = []

        def add_reference(candidate):
            if candidate == path and not inserted:
                inserted.append(Photo.objects.create(
                    dataset=self.dataset, uploader=self.admin, name='old.jpg',
                    sha='test', status='submitted', file='old.jpg',
                ))
            return original(candidate)

        with patch.object(Path, 'is_file', add_reference):
            result = self.cleanup(report)
        self.assertTrue(inserted)
        self.assertEqual(result['deleted'], [])
        self.assertEqual(path.read_bytes(), b'old!')

    def test_replacement_after_verification_is_restored_not_deleted(self):
        path, report = self.orphan()
        original = Path.rename
        changed = []

        def replace_before_rename(candidate, target):
            if candidate == path and not changed:
                changed.append(True)
                path.write_bytes(b'new!')
            return original(candidate, target)

        with patch.object(Path, 'rename', replace_before_rename):
            result = self.cleanup(report)
        self.assertEqual(result['deleted'], [])
        self.assertEqual(path.read_bytes(), b'new!')

    def test_reconcile_revocation_before_persist_does_not_publish_report(self):
        job = create_job(self.admin, 'reconcile', {})
        original = compute_capacity_report

        def revoke(**kwargs):
            result = original(**kwargs)
            User.objects.filter(id=self.admin.id).update(is_superuser=False)
            return result

        with patch('core.reconcile.compute_capacity_report', side_effect=revoke):
            process_one_job('review-revocation')
        job.refresh_from_db()
        self.assertEqual(job.state, 'failed')
        self.assertIn('資格', job.error)
        self.assertFalse(StorageReconcileReport.objects.filter(job=job).exists())
        self.assertTrue(Audit.objects.filter(action='job_auth_aborted', target=str(job.id)).exists())

    def test_inactive_creator_abort_is_audited(self):
        job = create_job(self.admin, 'reconcile', {})
        User.objects.filter(id=self.admin.id).update(is_active=False)
        process_one_job('review-inactive')
        job.refresh_from_db()
        self.assertEqual(job.state, 'failed')
        self.assertTrue(Audit.objects.filter(actor=self.admin, action='job_auth_aborted', target=str(job.id)).exists())

    def test_synchronous_scan_checks_authorization_before_publication(self):
        self.client.force_login(self.admin)
        original = compute_capacity_report

        def revoke(**kwargs):
            result = original(**kwargs)
            User.objects.filter(id=self.admin.id).update(is_superuser=False)
            return result

        with patch('core.reconcile.compute_capacity_report', side_effect=revoke):
            response = self.client.post('/api/reconcile/run', {}, content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(StorageReconcileReport.objects.exists())
        self.assertTrue(Audit.objects.filter(actor=self.admin, action='reconcile_auth_aborted').exists())

    def run_model_retry(self, legacy=False):
        photo = Photo.objects.create(dataset=self.dataset, uploader=self.admin, name='model.jpg', sha='src', status='submitted')
        vector = [1.0] + [0.0] * (DIMENSIONS - 1)
        PhotoEmbedding.objects.create(photo=photo, model_key=MODEL_KEY, source_sha='src', state='ready', vector=vector)
        PhotoOcr.objects.create(photo=photo, model_key=OCR_KEY, source_sha='src', state='ready', text='old')
        job = create_job(self.admin, 'semantic_index', {'datasets': [self.dataset.id], 'target': 'all', 'mode': 'rebuild'})
        with patch('core.jobs.semantic_readiness', return_value=True), patch('core.jobs.ocr_readiness', return_value=False), patch(
            'core.semantic.image_vectors', side_effect=lambda rows: [{'id': p.id, 'vector': vector} for p in rows]
        ):
            process_one_job('review-first')
        job.refresh_from_db()
        self.assertEqual(job.state, 'partial_failed')
        if legacy:
            job.cursor = {'cleared': True, 'sem_done': 1, 'ocr_done': 0}
            job.save(update_fields=['cursor'])
        retry_job(self.admin, job.id)
        with patch('core.jobs.semantic_readiness', return_value=True), patch('core.jobs.ocr_readiness', return_value=True), patch(
            'core.semantic.image_vectors', side_effect=lambda rows: [{'id': p.id, 'vector': vector} for p in rows]
        ), patch('core.ocr.ocr_images', side_effect=lambda rows: [{'id': p.id, 'text': 'new'} for p in rows]) as model:
            process_one_job('review-retry')
        job.refresh_from_db()
        self.assertEqual(job.state, 'completed')
        self.assertEqual(job.total, job.completed_count)
        self.assertEqual(job.failed_count, 0)
        self.assertEqual(PhotoOcr.objects.get(photo=photo).text, 'new')
        self.assertEqual(model.call_count, 1)

    def test_rebuild_retry_prepares_each_model_independently(self):
        self.run_model_retry()

    def test_legacy_partial_job_can_resume_without_losing_pending_model(self):
        self.run_model_retry(legacy=True)
