"""Non-destructive file reconciliation, preview repair, and storage capacity accounting."""
from datetime import timedelta
import hashlib
import io
import os
from pathlib import Path
import shutil
import uuid
from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models import Count, F
from django.utils import timezone
from .helpers import Problem, audit
from .models import (
    Anomaly,
    BackgroundJob,
    Dataset,
    Department,
    Export,
    FacePhotoIndex,
    PersonReference,
    Photo,
    PhotoEmbedding,
    PhotoOcr,
    StorageReconcileReport,
)
from .ocr_engine import MODEL_KEY as OCR_MODEL_KEY
from .semantic_engine import MODEL_KEY as SEMANTIC_MODEL_KEY


def _rel_norm(path_str):
    return str(path_str or '').replace('\\', '/').lstrip('/')


def _file_size(media_root, rel):
    if not rel:
        return 0
    full = media_root / rel
    try:
        return full.stat().st_size if full.is_file() else 0
    except OSError:
        return 0


def compute_capacity_report():
    """Return authorized department/dataset capacity, photo state counts, and storage space."""
    media_root = Path(settings.MEDIA_ROOT).resolve()
    depts = {d.id: d.name for d in Department.objects.all()}
    datasets = {
        d.id: {'id': d.id, 'name': d.name, 'department_id': d.item.department_id, 'department_name': depts.get(d.item.department_id, '')}
        for d in Dataset.objects.select_related('item').all()
    }

    by_ds = {
        did: {
            **info,
            'original_bytes': 0,
            'preview_bytes': 0,
            'submitted': 0,
            'draft': 0,
            'recycled': 0,
        }
        for did, info in datasets.items()
    }
    by_dept = {
        did: {
            'id': did,
            'name': dname,
            'original_bytes': 0,
            'preview_bytes': 0,
            'submitted': 0,
            'draft': 0,
            'recycled': 0,
        }
        for did, dname in depts.items()
    }

    for p in Photo.objects.values('dataset_id', 'status', 'file', 'preview').iterator(chunk_size=1000):
        ds_entry = by_ds.get(p['dataset_id'])
        if not ds_entry:
            continue
        orig_sz = _file_size(media_root, p['file'])
        prev_sz = _file_size(media_root, p['preview'])
        ds_entry['original_bytes'] += orig_sz
        ds_entry['preview_bytes'] += prev_sz
        st = p['status'] if p['status'] in ('submitted', 'draft', 'recycled') else 'draft'
        ds_entry[st] += 1
        dep_entry = by_dept.get(ds_entry['department_id'])
        if dep_entry:
            dep_entry['original_bytes'] += orig_sz
            dep_entry['preview_bytes'] += prev_sz
            dep_entry[st] += 1

    exports_bytes = sum(_file_size(media_root, e) for e in Export.objects.values_list('file', flat=True))
    people_ref_bytes = sum(_file_size(media_root, r) for r in PersonReference.objects.values_list('file', flat=True))

    external_temp_bytes = 0
    ext_dir = Path(settings.BASE_DIR) / '.external-gateway' / 'files'
    if ext_dir.is_dir():
        for root, _, files in os.walk(ext_dir):
            for fname in files:
                try:
                    external_temp_bytes += (Path(root) / fname).stat().st_size
                except OSError:
                    pass

    try:
        free_bytes = shutil.disk_usage(media_root).free if media_root.exists() else shutil.disk_usage(settings.BASE_DIR).free
    except OSError:
        free_bytes = None

    latest_report = StorageReconcileReport.objects.order_by('-created').values('id', 'created', 'summary').first()
    unresolved_anomalies = Anomaly.objects.filter(state='pending').count()

    return {
        'departments': sorted(by_dept.values(), key=lambda x: x['id']),
        'datasets': sorted(by_ds.values(), key=lambda x: x['id']),
        'derived_bytes': {
            'exports_bytes': exports_bytes,
            'people_ref_bytes': people_ref_bytes,
            'external_temp_bytes': external_temp_bytes,
        },
        'free_bytes': free_bytes,
        'unresolved_anomalies': unresolved_anomalies,
        'latest_reconcile_at': latest_report['created'].isoformat() if latest_report else None,
        'latest_reconcile_summary': latest_report['summary'] if latest_report else None,
    }


def run_reconciliation(actor=None, *, grace_hours=24, job=None, verify_hashes=True):
    """Run non-destructive storage & index reconciliation and persist a report."""
    grace_hours = max(0, min(720, int(grace_hours)))
    now = timezone.now()
    cutoff_ts = (now - timedelta(hours=grace_hours)).timestamp()
    media_root = Path(settings.MEDIA_ROOT).resolve()

    issues = []
    counts = {
        'missing_original': 0,
        'missing_preview': 0,
        'orphan_file': 0,
        'hash_mismatch': 0,
        'semantic_gap': 0,
        'ocr_gap': 0,
        'face_gap': 0,
        'external_stale': 0,
    }

    def add_issue(kind, **detail):
        counts[kind] += 1
        if len(issues) < 300:
            issues.append({'kind': kind, **detail})

    referenced_rels = set()
    active_job_photos = set()
    for scope in BackgroundJob.objects.filter(state__in=('pending', 'running')).values_list('scope', flat=True):
        if isinstance(scope, dict):
            for pid in scope.get('photo_ids') or []:
                active_job_photos.add(pid)

    # 1, 2, 4: Photo file integrity checks
    for p in Photo.objects.values('id', 'name', 'sha', 'status', 'dataset_id', 'file', 'preview', 'created').iterator(chunk_size=500):
        f_rel = _rel_norm(p['file'])
        p_rel = _rel_norm(p['preview'])
        if f_rel:
            referenced_rels.add(f_rel)
        if p_rel:
            referenced_rels.add(p_rel)

        # Skip in-flight newly created drafts or active job targets for missing checks if within grace period
        orig_path = media_root / f_rel if f_rel else None
        if not f_rel or not orig_path or not orig_path.is_file():
            add_issue('missing_original', photo_id=p['id'], name=p['name'], path=f_rel)
        elif verify_hashes and p['sha']:
            try:
                actual_sha = hashlib.sha256(orig_path.read_bytes()).hexdigest()
                if actual_sha != p['sha']:
                    add_issue('hash_mismatch', photo_id=p['id'], name=p['name'], expected=p['sha'], actual=actual_sha)
            except OSError:
                add_issue('missing_original', photo_id=p['id'], name=p['name'], path=f_rel)

        prev_path = media_root / p_rel if p_rel else None
        if not p_rel or not prev_path or not prev_path.is_file():
            add_issue('missing_preview', photo_id=p['id'], name=p['name'], path=p_rel, reason='縮圖不存在')
        else:
            try:
                with Image.open(prev_path) as im:
                    im.verify()
            except (OSError, UnidentifiedImageError, ValueError):
                add_issue('missing_preview', photo_id=p['id'], name=p['name'], path=p_rel, reason='縮圖無法讀取')

    for rel in Export.objects.values_list('file', flat=True):
        if rel:
            referenced_rels.add(_rel_norm(rel))
    for rel in PersonReference.objects.values_list('file', flat=True):
        if rel:
            referenced_rels.add(_rel_norm(rel))

    # 3: Orphan files under MEDIA_ROOT (excluding hidden control files, active jobs, and files within grace window)
    if media_root.is_dir():
        for root, _, files in os.walk(media_root):
            for fname in files:
                if fname.startswith('.'):
                    continue
                full = Path(root) / fname
                rel = _rel_norm(full.relative_to(media_root))
                if rel in referenced_rels:
                    continue
                try:
                    st = full.stat()
                except OSError:
                    continue
                if grace_hours > 0 and st.st_mtime >= cutoff_ts:
                    continue
                add_issue('orphan_file', path=rel, size=st.st_size)

    # 5: Content (semantic + OCR) index gaps on submitted photos
    submitted_q = Photo.objects.filter(status='submitted')
    ready_sem = set(
        PhotoEmbedding.objects.filter(
            model_key=SEMANTIC_MODEL_KEY, state='ready', source_sha=F('photo__sha')
        ).values_list('photo_id', flat=True)
    )
    for pid in submitted_q.exclude(id__in=ready_sem).values_list('id', flat=True)[:200]:
        add_issue('semantic_gap', photo_id=pid, model=SEMANTIC_MODEL_KEY)
    counts['semantic_gap'] = submitted_q.exclude(id__in=ready_sem).count()

    ready_ocr = set(
        PhotoOcr.objects.filter(
            model_key=OCR_MODEL_KEY, state='ready', source_sha=F('photo__sha')
        ).values_list('photo_id', flat=True)
    )
    for pid in submitted_q.exclude(id__in=ready_ocr).values_list('id', flat=True)[:200]:
        add_issue('ocr_gap', photo_id=pid, model=OCR_MODEL_KEY)
    counts['ocr_gap'] = submitted_q.exclude(id__in=ready_ocr).count()

    # 6: Face index failed/outdated checks (where face indexing has been run)
    stale_face_q = FacePhotoIndex.objects.exclude(state='ready', source_sha=F('photo__sha'))
    counts['face_gap'] = stale_face_q.count()
    for row in stale_face_q.values('photo_id', 'state', 'error')[:100]:
        add_issue('face_gap', photo_id=row['photo_id'], state=row['state'], error=row['error'])

    # 7: External gateway stalled receipts/grants
    from .models import ExternalImportReceipt
    stale_ext_q = ExternalImportReceipt.objects.filter(state='importing', updated__lt=now - timedelta(hours=max(1, grace_hours)))
    counts['external_stale'] = stale_ext_q.count()
    for row in stale_ext_q.values('id', 'grant_id', 'updated')[:100]:
        add_issue('external_stale', receipt_id=str(row['id']), grant_id=str(row['grant_id']))

    summary = {
        'counts': counts,
        'total_issues': sum(counts.values()),
        'grace_hours': grace_hours,
        'capacity': compute_capacity_report(),
        'non_destructive': True,
    }
    report = StorageReconcileReport.objects.create(
        actor=actor,
        job=job,
        mode='report',
        grace_hours=grace_hours,
        summary=summary,
        issues=issues,
    )
    if actor:
        audit(actor, 'reconcile_report', report.id, {'counts': counts, 'grace_hours': grace_hours})
    return report


def repair_missing_previews(actor, photo_ids=None):
    """Safely regenerate missing/broken thumbnails from valid original files without deleting anything."""
    q = Photo.objects.exclude(file='')
    if photo_ids:
        q = q.filter(id__in=photo_ids)
    repaired = 0
    failed = 0
    for p in q.iterator(chunk_size=100):
        need_repair = False
        if not p.preview:
            need_repair = True
        else:
            try:
                with p.preview.open('rb') as fp, Image.open(fp) as im:
                    im.verify()
            except Exception:
                need_repair = True
        if not need_repair:
            continue
        try:
            with p.file.open('rb') as src, Image.open(src) as orig:
                im = orig.convert('RGB')
                im.thumbnail((1800, 1800))
                buf = io.BytesIO()
                im.save(buf, 'JPEG', quality=88)
            p.preview.save(f'{uuid.uuid4().hex}.jpg', ContentFile(buf.getvalue()), save=True)
            repaired += 1
        except Exception:
            failed += 1
    audit(actor, 'reconcile_repair_previews', repaired, {'repaired': repaired, 'failed': failed})
    return {'repaired': repaired, 'failed': failed}


def confirm_orphan_cleanup(actor, paths, reason, grace_hours=24):
    """Delete explicitly confirmed orphan files only after re-checking DB refs and grace period."""
    reason = str(reason or '').strip()
    if not reason:
        raise Problem('請填寫清理確認理由', 422)
    if not isinstance(paths, list) or not paths:
        raise Problem('請勾選要清理的孤立檔案路徑', 422)

    grace_hours = max(1, int(grace_hours))
    cutoff_ts = (timezone.now() - timedelta(hours=grace_hours)).timestamp()
    media_root = Path(settings.MEDIA_ROOT).resolve()

    referenced = set()
    for rel in Photo.objects.values_list('file', flat=True):
        if rel:
            referenced.add(_rel_norm(rel))
    for rel in Photo.objects.values_list('preview', flat=True):
        if rel:
            referenced.add(_rel_norm(rel))
    for rel in Export.objects.values_list('file', flat=True):
        if rel:
            referenced.add(_rel_norm(rel))
    for rel in PersonReference.objects.values_list('file', flat=True):
        if rel:
            referenced.add(_rel_norm(rel))

    deleted = []
    skipped = []
    for raw_rel in paths[:200]:
        rel = _rel_norm(raw_rel)
        if not rel or '..' in Path(rel).parts or rel in referenced:
            skipped.append({'path': rel, 'reason': '目前仍有資料庫引用或路徑無效'})
            continue
        full = (media_root / rel).resolve()
        try:
            full.relative_to(media_root)
        except ValueError:
            skipped.append({'path': rel, 'reason': '超出儲存目錄範圍'})
            continue
        if not full.is_file():
            skipped.append({'path': rel, 'reason': '檔案已不存在'})
            continue
        if full.stat().st_mtime >= cutoff_ts:
            skipped.append({'path': rel, 'reason': f'尚未超過 {grace_hours} 小時寬限期'})
            continue
        full.unlink()
        deleted.append(rel)

    audit(actor, 'reconcile_orphan_cleanup', len(deleted), {'deleted': deleted, 'skipped': skipped, 'reason': reason})
    return {'deleted': deleted, 'skipped': skipped}
