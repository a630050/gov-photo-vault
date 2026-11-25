"""Unified persistent background job center and lease-based worker runner."""
import csv
from datetime import timedelta
import io
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile
from django.core.files import File
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from .geocoding import reverse_geocode
from .helpers import Problem, audit, manager, visible
from .models import BackgroundJob, Dataset, Export, Photo, PhotoEmbedding, PhotoOcr, SimilarityPair, SimilarityRun
from .ocr import MODEL_KEY as OCR_MODEL_KEY, index_ocr_batch
from .ocr_engine import ModelUnavailable as OcrUnavailable, readiness as ocr_readiness
from .permissions import allowed
from .search import resolve_selection_ids
from .semantic import MODEL_KEY as SEMANTIC_MODEL_KEY, index_batch
from .semantic_engine import ModelUnavailable as SemanticUnavailable, readiness as semantic_readiness
from .similarity import ALGORITHM as SIMILARITY_ALGORITHM, MAX_PHOTOS as SIMILARITY_MAX_PHOTOS, THRESHOLDS as SIMILARITY_THRESHOLDS, distances as sim_distances, fingerprint as sim_fingerprint, snapshot as sim_snapshot

LEASE_SECONDS = 60
MAX_CONCURRENT_JOBS = 2
MAX_EXPORT_JOB_PHOTOS = 5000
HEAVY_JOB_KINDS = ('semantic_index', 'similarity')
ACTIVE_STATES = ('pending', 'running')
RETRYABLE_STATES = ('failed', 'partial_failed', 'cancelled')


def serialize_job(job):
    return {
        'id': str(job.id),
        'kind': job.kind,
        'actor': job.actor.username,
        'dataset': job.dataset_id,
        'dataset_name': job.dataset.name if job.dataset_id else '',
        'scope': job.scope,
        'params': job.params,
        'algorithm': job.algorithm,
        'state': job.state,
        'total': job.total,
        'completed_count': job.completed_count,
        'failed_count': job.failed_count,
        'cancel_requested': job.cancel_requested,
        'worker_id': job.worker_id,
        'lease_until': job.lease_until.isoformat() if job.lease_until else None,
        'result_data': job.result_data,
        'error': job.error,
        'created': job.created.isoformat(),
        'started_at': job.started_at.isoformat() if job.started_at else None,
        'finished_at': job.finished_at.isoformat() if job.finished_at else None,
        'updated': job.updated.isoformat(),
    }


def can_view_job(user, job):
    if user.is_superuser or job.actor_id == user.id:
        return True
    if job.dataset_id and manager(user, job.dataset.item.department_id):
        return True
    return False


def _job_scope_queryset(user):
    qs = BackgroundJob.objects.select_related('actor', 'dataset__item')
    if user.is_superuser:
        return qs
    managed_depts = [d for d in set(user.membership_set.filter(role='supervisor').values_list('department_id', flat=True))]
    return qs.filter(Q(actor=user) | Q(dataset__item__department_id__in=managed_depts))


def create_job(user, kind, data):
    if kind not in ('export', 'similarity', 'region_recompute', 'semantic_index'):
        raise Problem('未知的工作類型', 422)

    if kind == 'export':
        if data.get('selection_token'):
            ids = resolve_selection_ids(visible(user), user, data['selection_token'], data.get('excluded_photos'))
        else:
            raw_ids = data.get('photos', [])
            if not isinstance(raw_ids, list) or not raw_ids or any(type(i) is not int for i in raw_ids):
                raise Problem('請選擇要匯出的照片', 422)
            ids = list(dict.fromkeys(raw_ids))
        if not ids:
            raise Problem('目前條件沒有可匯出的照片', 422)
        if len(ids) > MAX_EXPORT_JOB_PHOTOS:
            raise Problem(f'單次背景匯出上限 {MAX_EXPORT_JOB_PHOTOS} 張，目前選取 {len(ids)} 張', 422)
        photos = list(visible(user).filter(id__in=ids, status='submitted').select_related('dataset__item'))
        if len(photos) != len(ids):
            raise Problem('部分照片狀態或權限已變動，請重新確認', 409)
        if any(
            not allowed(user, p.dataset.item.department_id, 'export')
            or not allowed(user, p.dataset.item.department_id, 'download')
            for p in photos
        ):
            raise Problem('需要批次匯出及原圖下載權限', 403)
        ds_ids = sorted({p.dataset_id for p in photos})
        ds = photos[0].dataset if len(ds_ids) == 1 else None
        job = BackgroundJob.objects.create(
            kind='export',
            actor=user,
            dataset=ds,
            scope={'photo_ids': ids, 'datasets': ds_ids},
            params={'count': len(ids)},
            algorithm='export-zip-v1',
            total=len(ids),
        )
        audit(user, 'job_create', job.id, {'kind': 'export', 'total': len(ids)})
        return job

    if kind == 'similarity':
        ds_id = data.get('dataset')
        if not ds_id:
            raise Problem('近似分析只能針對單一資料集執行', 422)
        ds = Dataset.objects.select_related('item').filter(id=int(ds_id)).first()
        if not ds or not allowed(user, ds.item.department_id, 'analyze'):
            raise Problem('沒有該資料集的近似分析權限', 403)
        if ds.archived:
            raise Problem('資料集已封存，不能發起分析', 409)
        hours = data.get('hours', 24)
        if isinstance(hours, bool):
            raise Problem('時間門檻格式錯誤', 422)
        hours = float(hours)
        if not math.isfinite(hours) or not 0 <= hours <= 876000:
            raise Problem('時間門檻須為 0 至 876000 小時', 422)
        photos_q = visible(user).filter(dataset=ds, status='submitted').order_by('id')
        raw_ids = data.get('photos')
        if raw_ids is not None:
            if not isinstance(raw_ids, list) or not raw_ids or any(type(i) is not int for i in raw_ids):
                raise Problem('請選擇有效照片', 422)
            photos_q = photos_q.filter(id__in=raw_ids)
            if photos_q.count() != len(set(raw_ids)):
                raise Problem('選取照片含其他資料集或不可用照片', 409)
        rows = list(photos_q[: SIMILARITY_MAX_PHOTOS + 1])
        if not 2 <= len(rows) <= SIMILARITY_MAX_PHOTOS:
            raise Problem(f'單次近似分析須選 2 至 {SIMILARITY_MAX_PHOTOS} 張正式照片', 422)
        members = [sim_snapshot(p) for p in rows]

        with transaction.atomic():
            job = BackgroundJob.objects.create(
                kind='similarity',
                actor=user,
                dataset=ds,
                scope={'dataset': ds.id, 'photo_ids': [p.id for p in rows]},
                params={'hours': hours, 'thresholds': SIMILARITY_THRESHOLDS},
                algorithm=SIMILARITY_ALGORITHM,
                total=len(rows),
            )
            run = SimilarityRun.objects.create(
                dataset=ds,
                actor=user,
                job=job,
                hours=hours,
                algorithm=SIMILARITY_ALGORITHM,
                thresholds=SIMILARITY_THRESHOLDS,
                members=members,
            )
            job.result_data = {'similarity_run_id': run.id}
            job.save(update_fields=['result_data'])
            audit(user, 'job_create', job.id, {'kind': 'similarity', 'dataset': ds.id, 'run': run.id})
        return job

    if kind == 'region_recompute':
        ds_id = data.get('dataset')
        ds = None
        if ds_id:
            ds = Dataset.objects.select_related('item').filter(id=int(ds_id)).first()
            if not ds:
                raise Problem('找不到資料集', 404)
            if not manager(user, ds.item.department_id):
                raise Problem('需要主管或系統管理者權限', 403)
            q = Photo.objects.filter(dataset=ds)
        else:
            if not user.is_superuser:
                raise Problem('僅系統管理者可執行全系統行政區重算', 403)
            q = Photo.objects.all()
        only_failed = bool(data.get('only_failed'))
        q = q.filter(lat__isnull=False, lon__isnull=False)
        if only_failed:
            q = q.filter(admin_county='')
        total = q.count()
        if total == 0:
            raise Problem('此範圍目前沒有需要重算行政區的照片', 422)
        job = BackgroundJob.objects.create(
            kind='region_recompute',
            actor=user,
            dataset=ds,
            scope={'dataset': ds.id if ds else None},
            params={'only_failed': only_failed},
            algorithm='taiwan-village-topo-v1',
            total=total,
        )
        audit(user, 'job_create', job.id, {'kind': 'region_recompute', 'dataset': ds.id if ds else None, 'total': total})
        return job

    if kind == 'semantic_index':
        if not user.is_superuser:
            raise Problem('僅系統管理者可排程內容索引工作', 403)
        target = str(data.get('target', 'all'))
        mode = str(data.get('mode', 'build'))
        if target not in ('all', 'semantic', 'ocr'):
            raise Problem('未知索引類型', 422)
        if mode not in ('build', 'repair', 'rebuild'):
            raise Problem('未知索引模式', 422)
        ds_ids = data.get('datasets') or []
        if not isinstance(ds_ids, list) or any(type(x) is not int for x in ds_ids):
            raise Problem('資料集條件格式錯誤', 422)
        base_q = Photo.objects.filter(status='submitted')
        if ds_ids:
            base_q = base_q.filter(dataset_id__in=ds_ids)
        total = base_q.count()
        job = BackgroundJob.objects.create(
            kind='semantic_index',
            actor=user,
            dataset=Dataset.objects.filter(id=ds_ids[0]).first() if len(ds_ids) == 1 else None,
            scope={'datasets': ds_ids},
            params={'target': target, 'mode': mode},
            algorithm=f'{SEMANTIC_MODEL_KEY}+{OCR_MODEL_KEY}',
            total=total,
        )
        audit(user, 'job_create', job.id, {'kind': 'semantic_index', 'target': target, 'mode': mode})
        return job


def cancel_job(user, job_id):
    with transaction.atomic():
        job = BackgroundJob.objects.select_for_update().select_related('dataset__item', 'actor').filter(id=job_id).first()
        if not job:
            raise Problem('找不到工作紀錄', 404)
        if not can_view_job(user, job):
            raise Problem('沒有權限操作此工作', 403)
        if job.state not in ACTIVE_STATES:
            raise Problem('此工作已結束，無法取消', 409)
        now = timezone.now()
        job.cancel_requested = True
        if job.state == 'pending':
            job.state = 'cancelled'
            job.finished_at = now
            job.lease_until = None
            job.save(update_fields=['cancel_requested', 'state', 'finished_at', 'lease_until', 'updated'])
            if job.kind == 'similarity':
                SimilarityRun.objects.filter(job=job, state__in=ACTIVE_STATES).update(state='cancelled')
        else:
            job.save(update_fields=['cancel_requested', 'updated'])
        audit(user, 'job_cancel', job.id, {'kind': job.kind, 'state': job.state})
        return job


def retry_job(user, job_id):
    with transaction.atomic():
        job = BackgroundJob.objects.select_for_update().select_related('dataset__item', 'actor').filter(id=job_id).first()
        if not job:
            raise Problem('找不到工作紀錄', 404)
        if not can_view_job(user, job):
            raise Problem('沒有權限重試此工作', 403)
        if job.state not in RETRYABLE_STATES:
            raise Problem('目前工作狀態不可重試', 409)
        if not job.actor.is_active:
            raise Problem('建立者帳號已停用，無法重試', 403)

        # Re-verify permissions on retry
        if job.kind == 'export':
            ids = job.scope.get('photo_ids', [])
            photos = list(visible(job.actor).filter(id__in=ids, status='submitted').select_related('dataset__item'))
            if len(photos) != len(ids):
                raise Problem('匯出目標照片狀態或權限已變動，無法重試', 409)
            if any(
                not allowed(job.actor, p.dataset.item.department_id, 'export')
                or not allowed(job.actor, p.dataset.item.department_id, 'download')
                for p in photos
            ):
                raise Problem('已無匯出或原圖下載權限', 403)
        elif job.kind == 'similarity':
            if not job.dataset_id or not allowed(job.actor, job.dataset.item.department_id, 'analyze'):
                raise Problem('已無資料集近似分析權限', 403)
            if job.dataset.archived:
                raise Problem('資料集已封存，無法重試分析', 409)
            run = SimilarityRun.objects.filter(job=job).first()
            if run:
                run.state = 'running' if run.fingerprints else 'pending'
                run.error = ''
                run.save(update_fields=['state', 'error', 'updated'])
        elif job.kind == 'region_recompute':
            if job.dataset_id:
                if not manager(job.actor, job.dataset.item.department_id):
                    raise Problem('已無資料集主管權限', 403)
            elif not job.actor.is_superuser:
                raise Problem('已無系統管理者權限', 403)
        elif job.kind == 'semantic_index':
            if not job.actor.is_superuser:
                raise Problem('已無系統管理者權限', 403)

        job.state = 'pending'
        job.cancel_requested = False
        job.lease_until = None
        job.worker_id = ''
        job.error = ''
        job.finished_at = None
        job.save(update_fields=['state', 'cancel_requested', 'lease_until', 'worker_id', 'error', 'finished_at', 'updated'])
        audit(user, 'job_retry', job.id, {'kind': job.kind})
        return job


def _renew_and_check_cancel(job, worker_id):
    """Renew worker lease and return True if cancellation was requested or lease lost."""
    now = timezone.now()
    fresh = BackgroundJob.objects.filter(id=job.id).values('cancel_requested', 'worker_id', 'state').first()
    if not fresh or fresh['state'] != 'running' or fresh['worker_id'] != worker_id:
        return 'lost'
    if fresh['cancel_requested']:
        return 'cancelled'
    BackgroundJob.objects.filter(id=job.id, worker_id=worker_id, state='running').update(
        lease_until=now + timedelta(seconds=LEASE_SECONDS),
        updated=now,
    )
    return 'ok'


def _mark_job_done(job, worker_id, state, *, completed_count=None, failed_count=None, result_data=None, error=''):
    now = timezone.now()
    updates = {
        'state': state,
        'lease_until': None,
        'finished_at': now,
        'updated': now,
        'error': error,
    }
    if completed_count is not None:
        updates['completed_count'] = completed_count
    if failed_count is not None:
        updates['failed_count'] = failed_count
    if result_data is not None:
        updates['result_data'] = result_data
    BackgroundJob.objects.filter(id=job.id, worker_id=worker_id).update(**updates)
    job.refresh_from_db()


def _execute_export_job(job, worker_id):
    actor = job.actor
    if not actor.is_active:
        _mark_job_done(job, worker_id, 'failed', error='執行者帳號已停用')
        return

    # Idempotency guard: if an Export artifact was already committed for this job, reuse it cleanly.
    existing = Export.objects.filter(job=job).first()
    if existing:
        _mark_job_done(
            job,
            worker_id,
            'completed',
            completed_count=len(existing.photo_ids),
            failed_count=0,
            result_data={'export_id': existing.id, 'url': f'/exports/{existing.id}', 'count': len(existing.photo_ids)},
        )
        return

    ids = job.scope.get('photo_ids', [])
    if not ids:
        _mark_job_done(job, worker_id, 'failed', error='匯出清單為空')
        return

    def safe_csv(value):
        text = str(value)
        return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text

    csvout = io.StringIO()
    writer = csv.writer(csvout)
    writer.writerow(['id', 'name', 'dataset', 'captured', 'lat', 'lon', 'county', 'town', 'village', 'tags', 'attributes'])

    tmp_path = None
    completed = 0
    failed = 0
    failed_ids = []
    exported_ids = []
    try:
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            tmp_path = tmp.name
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as z:
            chunk_size = 50
            for start in range(0, len(ids), chunk_size):
                chk = _renew_and_check_cancel(job, worker_id)
                if chk == 'lost':
                    return
                if chk == 'cancelled':
                    _mark_job_done(job, worker_id, 'cancelled', completed_count=completed, failed_count=failed)
                    return

                chunk_ids = ids[start : start + chunk_size]
                actor.refresh_from_db()
                if not actor.is_active:
                    _mark_job_done(job, worker_id, 'failed', completed_count=completed, failed_count=failed, error='帳號已停用，匯出終止')
                    return
                photo_map = {
                    p.id: p
                    for p in visible(actor).filter(id__in=chunk_ids, status='submitted').select_related('dataset__item')
                }
                for pid in chunk_ids:
                    p = photo_map.get(pid)
                    if not p:
                        failed += 1
                        failed_ids.append(pid)
                        continue
                    if not allowed(actor, p.dataset.item.department_id, 'export') or not allowed(actor, p.dataset.item.department_id, 'download'):
                        _mark_job_done(job, worker_id, 'failed', completed_count=completed, failed_count=failed, error='匯出或下載權限已撤銷，已中止匯出')
                        return
                    try:
                        arcname = f'photos/{p.id}{Path(p.name).suffix}'
                        with p.file.open('rb') as src, z.open(arcname, 'w') as dst:
                            shutil.copyfileobj(src, dst, length=65536)
                        writer.writerow([
                            p.id,
                            safe_csv(p.name),
                            safe_csv(p.dataset.name),
                            p.captured.isoformat() if p.captured else '',
                            p.lat,
                            p.lon,
                            safe_csv(p.admin_county),
                            safe_csv(p.admin_town),
                            safe_csv(p.admin_village),
                            safe_csv(','.join(p.tags)),
                            safe_csv(json.dumps(p.attrs, ensure_ascii=False)),
                        ])
                        exported_ids.append(p.id)
                        completed += 1
                    except OSError:
                        failed += 1
                        failed_ids.append(pid)

                BackgroundJob.objects.filter(id=job.id, worker_id=worker_id).update(
                    completed_count=completed, failed_count=failed, updated=timezone.now()
                )
            z.writestr('metadata.csv', csvout.getvalue().encode('utf-8-sig'))

        if not exported_ids:
            _mark_job_done(job, worker_id, 'failed', completed_count=0, failed_count=failed, error='所有目標照片皆無法讀取或已失效')
            return

        with transaction.atomic():
            existing_again = Export.objects.filter(job=job).first()
            if existing_again:
                export_obj = existing_again
            else:
                export_obj = Export(actor=actor, job=job, photo_ids=exported_ids)
                with open(tmp_path, 'rb') as fp:
                    export_obj.file.save(f'{uuid.uuid4()}.zip', File(fp))
                audit(actor, 'export', export_obj.id, {'photos': exported_ids, 'job': str(job.id)})

        final_state = 'partial_failed' if failed > 0 else 'completed'
        _mark_job_done(
            job,
            worker_id,
            final_state,
            completed_count=completed,
            failed_count=failed,
            result_data={
                'export_id': export_obj.id,
                'url': f'/exports/{export_obj.id}',
                'count': len(exported_ids),
                'failed_ids': failed_ids[:100],
            },
            error=f'共有 {failed} 張照片無法讀取或已失權，其餘 {completed} 張已打包' if failed else '',
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _execute_similarity_job(job, worker_id):
    actor = job.actor
    run = SimilarityRun.objects.filter(job=job).select_related('dataset__item').first()
    if not run:
        _mark_job_done(job, worker_id, 'failed', error='找不到對應的近似分析紀錄')
        return

    while True:
        chk = _renew_and_check_cancel(job, worker_id)
        if chk == 'lost':
            return
        if chk == 'cancelled':
            run.state = 'cancelled'
            run.save(update_fields=['state', 'updated'])
            _mark_job_done(job, worker_id, 'cancelled', completed_count=len(run.fingerprints))
            return

        actor.refresh_from_db()
        run.refresh_from_db()
        if not actor.is_active or not allowed(actor, run.dataset.item.department_id, 'analyze'):
            run.state = 'failed'
            run.error = '執行者帳號停用或已無近似分析權限'
            run.save(update_fields=['state', 'error', 'updated'])
            _mark_job_done(job, worker_id, 'failed', completed_count=len(run.fingerprints), error=run.error)
            return
        if run.dataset.archived:
            run.state = 'cancelled'
            run.error = '資料集已封存，分析已停止'
            run.save(update_fields=['state', 'error', 'updated'])
            _mark_job_done(job, worker_id, 'cancelled', completed_count=len(run.fingerprints), error=run.error)
            return
        if run.algorithm != SIMILARITY_ALGORITHM:
            run.state = 'failed'
            run.error = '演算法版本已變更，請重新執行'
            run.save(update_fields=['state', 'error', 'updated'])
            _mark_job_done(job, worker_id, 'failed', completed_count=len(run.fingerprints), error=run.error)
            return

        current = {
            p.id: p
            for p in visible(actor).filter(dataset=run.dataset, status='submitted', id__in=[m['id'] for m in run.members])
        }
        if any(m['id'] not in current or sim_snapshot(current[m['id']]) != m for m in run.members):
            run.state = 'failed'
            run.error = '分析期間照片資料已變動，請重新執行'
            run.save(update_fields=['state', 'error', 'updated'])
            _mark_job_done(job, worker_id, 'failed', completed_count=len(run.fingerprints), error=run.error)
            return

        done_count = len(run.fingerprints)
        if done_count < len(run.members):
            batch = run.members[done_count : done_count + 10]
            new_fps = list(run.fingerprints)
            for m in batch:
                p = current[m['id']]
                try:
                    new_fps.append({'id': p.id, **sim_fingerprint(p.file)})
                except (OSError, ValueError):
                    new_fps.append({'id': p.id, 'error': '無法讀取或解碼原始照片'})
            run.fingerprints = new_fps
            run.state = 'running'
            run.save(update_fields=['fingerprints', 'state', 'updated'])
            failed_cnt = sum('error' in x for x in new_fps)
            BackgroundJob.objects.filter(id=job.id, worker_id=worker_id).update(
                completed_count=len(new_fps),
                failed_count=failed_cnt,
                updated=timezone.now(),
            )
            continue

        # All fingerprints computed -> create pairs idempotently
        valid_fps = [x for x in run.fingerprints if 'error' not in x]
        existing_pairs = set(SimilarityPair.objects.filter(run=run).values_list('a_id', 'b_id'))
        to_create = []
        for a, b in _iter_pairs(valid_fps):
            pa, pb = current[a['id']], current[b['id']]
            if (pa.id, pb.id) in existing_pairs or (pb.id, pa.id) in existing_pairs:
                continue
            score = sim_distances(a, b)
            if any(score[k] > run.thresholds[k] for k in score):
                continue
            gap = abs((pa.captured - pb.captured).total_seconds()) / 3600 if pa.captured and pb.captured else None
            flag = 'needs_time' if gap is None or not pa.time_confirmed or not pb.time_confirmed else 'over' if gap > run.hours else 'within'
            to_create.append(SimilarityPair(run=run, a=pa, b=pb, distances=score, gap_hours=gap, time_state=flag))

        with transaction.atomic():
            if to_create:
                SimilarityPair.objects.bulk_create(to_create, ignore_conflicts=True)
            run.state = 'completed'
            run.save(update_fields=['state', 'updated'])
            failed_cnt = sum('error' in x for x in run.fingerprints)
            total_pairs = run.pairs.count()
            audit(actor, 'similarity_complete', run.id, {'pairs': total_pairs, 'failed': failed_cnt, 'job': str(job.id)})

        _mark_job_done(
            job,
            worker_id,
            'partial_failed' if failed_cnt else 'completed',
            completed_count=len(run.fingerprints),
            failed_count=failed_cnt,
            result_data={'similarity_run_id': run.id, 'pairs': total_pairs},
            error=f'{failed_cnt} 張照片原圖無法讀取' if failed_cnt else '',
        )
        return


def _iter_pairs(items):
    from itertools import combinations
    return combinations(items, 2)


def _execute_region_recompute_job(job, worker_id):
    cursor_id = int(job.cursor.get('last_id', 0))
    completed = int(job.cursor.get('completed', 0))
    failed = int(job.cursor.get('failed', 0))
    only_failed = bool(job.params.get('only_failed'))

    base_q = Photo.objects.filter(lat__isnull=False, lon__isnull=False)
    if job.dataset_id:
        base_q = base_q.filter(dataset_id=job.dataset_id)
    if only_failed:
        base_q = base_q.filter(admin_county='')

    while True:
        chk = _renew_and_check_cancel(job, worker_id)
        if chk == 'lost':
            return
        if chk == 'cancelled':
            _mark_job_done(job, worker_id, 'cancelled', completed_count=completed, failed_count=failed)
            return

        batch = list(base_q.filter(id__gt=cursor_id).order_by('id').values('id', 'lat', 'lon')[:50])
        if not batch:
            break
        for row in batch:
            lat, lon = row['lat'], row['lon']
            try:
                county, town, village = reverse_geocode(lat, lon)
                # Guard against stale overwrite if coordinates changed while job was running
                Photo.objects.filter(id=row['id'], lat=lat, lon=lon).update(
                    admin_county=county,
                    admin_town=town,
                    admin_village=village,
                )
                completed += 1
            except Exception:
                failed += 1
            cursor_id = row['id']

        BackgroundJob.objects.filter(id=job.id, worker_id=worker_id).update(
            cursor={'last_id': cursor_id, 'completed': completed, 'failed': failed},
            completed_count=completed,
            failed_count=failed,
            updated=timezone.now(),
        )

    final_state = 'partial_failed' if failed > 0 else 'completed'
    _mark_job_done(
        job,
        worker_id,
        final_state,
        completed_count=completed,
        failed_count=failed,
        result_data={'processed': completed, 'failed': failed},
        error=f'{failed} 筆行政區反查失敗' if failed else '',
    )


def _execute_semantic_index_job(job, worker_id):
    target = job.params.get('target', 'all')
    mode = job.params.get('mode', 'build')
    ds_ids = job.scope.get('datasets') or []

    if mode == 'rebuild' and not job.cursor.get('cleared'):
        if target in ('all', 'semantic'):
            q_emb = PhotoEmbedding.objects.filter(model_key=SEMANTIC_MODEL_KEY)
            if ds_ids:
                q_emb = q_emb.filter(photo__dataset_id__in=ds_ids)
            q_emb.delete()
        if target in ('all', 'ocr'):
            q_ocr = PhotoOcr.objects.filter(model_key=OCR_MODEL_KEY)
            if ds_ids:
                q_ocr = q_ocr.filter(photo__dataset_id__in=ds_ids)
            q_ocr.delete()
        BackgroundJob.objects.filter(id=job.id, worker_id=worker_id).update(
            cursor={'cleared': True, 'rounds': 0}, updated=timezone.now()
        )

    retry_failed = mode in ('repair', 'rebuild')
    total_sem = int(job.cursor.get('sem_done', 0))
    total_ocr = int(job.cursor.get('ocr_done', 0))
    errors = []

    while True:
        chk = _renew_and_check_cancel(job, worker_id)
        if chk == 'lost':
            return
        if chk == 'cancelled':
            _mark_job_done(job, worker_id, 'cancelled', completed_count=total_sem + total_ocr)
            return

        did_work = False
        if target in ('all', 'semantic') and semantic_readiness():
            try:
                cnt = index_batch(limit=5, retry=retry_failed)
                if cnt:
                    total_sem += cnt
                    did_work = True
            except SemanticUnavailable as exc:
                errors.append(f'語意模型不可用：{exc}')

        if target in ('all', 'ocr') and ocr_readiness():
            try:
                cnt = index_ocr_batch(limit=5, retry=retry_failed)
                if cnt:
                    total_ocr += cnt
                    did_work = True
            except OcrUnavailable as exc:
                errors.append(f'OCR 模型不可用：{exc}')

        retry_failed = False
        BackgroundJob.objects.filter(id=job.id, worker_id=worker_id).update(
            completed_count=total_sem + total_ocr,
            cursor={'cleared': True, 'sem_done': total_sem, 'ocr_done': total_ocr},
            updated=timezone.now(),
        )
        if not did_work:
            break

    if errors and (total_sem + total_ocr) == 0:
        _mark_job_done(job, worker_id, 'failed', completed_count=0, failed_count=1, error='；'.join(dict.fromkeys(errors)))
        return

    _mark_job_done(
        job,
        worker_id,
        'partial_failed' if errors else 'completed',
        completed_count=total_sem + total_ocr,
        failed_count=len(errors),
        result_data={'semantic_processed': total_sem, 'ocr_processed': total_ocr},
        error='；'.join(dict.fromkeys(errors)),
    )


def claim_next_job(worker_id):
    now = timezone.now()
    with transaction.atomic():
        running_q = BackgroundJob.objects.filter(state='running', lease_until__gte=now)
        if running_q.count() >= MAX_CONCURRENT_JOBS:
            return None
        heavy_busy = running_q.filter(kind__in=HEAVY_JOB_KINDS).exists()

        candidates = BackgroundJob.objects.filter(
            Q(state='pending') | Q(state='running', lease_until__lt=now)
        ).order_by('created')

        for cand in candidates:
            if cand.kind in HEAVY_JOB_KINDS and heavy_busy:
                continue
            updated = BackgroundJob.objects.filter(
                id=cand.id,
                state=cand.state,
                lease_until=cand.lease_until,
            ).update(
                state='running',
                worker_id=worker_id,
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
                started_at=cand.started_at or now,
                updated=now,
            )
            if updated == 1:
                cand.refresh_from_db()
                return cand
    return None


def run_job(job, worker_id):
    try:
        if job.kind == 'export':
            _execute_export_job(job, worker_id)
        elif job.kind == 'similarity':
            _execute_similarity_job(job, worker_id)
        elif job.kind == 'region_recompute':
            _execute_region_recompute_job(job, worker_id)
        elif job.kind == 'semantic_index':
            _execute_semantic_index_job(job, worker_id)
        else:
            _mark_job_done(job, worker_id, 'failed', error=f'不支援的工作類型：{job.kind}')
    except Exception as exc:
        _mark_job_done(job, worker_id, 'failed', error=f'工作執行發生未預期錯誤：{exc}')


def process_one_job(worker_id=None):
    wid = worker_id or f'worker-{os.getpid()}'
    job = claim_next_job(wid)
    if not job:
        return None
    run_job(job, wid)
    job.refresh_from_db()
    return job
