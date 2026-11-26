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
from django.db import connection, transaction
from django.db.models import Count, F, Q
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


PROTECTED_FILENAMES = {'.photo-storage-id', 'thumbs.db', 'desktop.ini'}
PROTECTED_SUFFIXES = ('.lock', '.lck', '.pid', '.part', '.tmp')


def _rel_norm(path_str):
    return str(path_str or '').replace('\\', '/').lstrip('/')


def _is_symlink_or_junction(path_obj):
    try:
        if path_obj.is_symlink() or os.path.islink(path_obj):
            return True
        if hasattr(os.path, 'isjunction') and os.path.isjunction(path_obj):
            return True
        st = os.lstat(path_obj)
        if getattr(st, 'st_file_attributes', 0) & 0x400:
            return True
    except OSError:
        return False
    return False


def is_protected_storage_rel(rel):
    """Return True if rel is invalid, traverses outside root, or names a protected control/runtime file."""
    if not rel or '\x00' in str(rel):
        return True
    raw = str(rel).replace('\\', '/')
    if raw.startswith('/') or os.path.splitdrive(raw)[0] or ':' in raw.split('/', 1)[0]:
        return True
    parts = Path(raw).parts
    if not parts:
        return True
    for part in parts:
        if not part or part in ('.', '..') or part.startswith('.'):
            return True
    lower = parts[-1].lower()
    if lower in PROTECTED_FILENAMES or lower.endswith(PROTECTED_SUFFIXES):
        return True
    return False


def _resolve_confined_path(media_root, rel):
    """Ensure every path segment under media_root is non-symlink/non-junction and resolves inside root."""
    if is_protected_storage_rel(rel):
        return None, '受保護檔案或路徑不合法'
    cur = media_root
    for part in Path(rel).parts:
        cur = cur / part
        if _is_symlink_or_junction(cur):
            return None, '偵測到符號連結或目錄連結，拒絕越界操作'
    try:
        resolved = cur.resolve()
        resolved.relative_to(media_root)
    except (OSError, ValueError):
        return None, '超出儲存目錄範圍'
    if _is_symlink_or_junction(resolved):
        return None, '拒絕存取連結檔案'
    return resolved, ''


def _file_size(media_root, rel):
    if not rel:
        return 0
    full = media_root / rel
    try:
        return full.stat().st_size if full.is_file() else 0
    except OSError:
        return 0


def _checked_rows(rows, checkpoint=None):
    for index, row in enumerate(rows):
        if checkpoint and index % 100 == 0:checkpoint()
        yield row
    if checkpoint:checkpoint()


def _file_identity(path):
    """Identify the actual object and content; timestamps alone can be preserved on copy."""
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise OSError('File changed during verification')
    return {'size': after.st_size, 'mtime_ns': after.st_mtime_ns,
            'inode': after.st_ino, 'device': after.st_dev, 'sha256': sha}


def _has_file_reference(rel):
    return (Photo.objects.filter(Q(file=rel) | Q(preview=rel)).exists()
            or Export.objects.filter(file=rel).exists()
            or PersonReference.objects.filter(file=rel).exists())


def compute_capacity_report(checkpoint=None):
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

    for p in _checked_rows(Photo.objects.values('dataset_id', 'status', 'file', 'preview').iterator(chunk_size=1000), checkpoint):
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
        for root, _, files in _checked_rows(os.walk(ext_dir), checkpoint):
            for fname in _checked_rows(files, checkpoint):
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


def run_reconciliation(actor=None, *, grace_hours=24, job=None, verify_hashes=True, checkpoint=None):
    """Run non-destructive storage & index reconciliation and persist a report."""
    # The synchronous admin endpoint needs the same live checks as a queued scan.
    if checkpoint is None and actor is not None:
        def checkpoint():
            fresh = actor.__class__.objects.filter(id=actor.id).first()
            if not fresh or not fresh.is_active or not fresh.is_superuser:
                reason = '操作者已停用或失去系統管理者資格，對帳已中止'
                audit(fresh, 'reconcile_auth_aborted', str(job.id) if job else 'run', {'reason': reason})
                raise Problem(reason, 403)
    if checkpoint:checkpoint()
    grace_hours = max(0, min(720, int(grace_hours)))
    now = timezone.now()
    grace_td = timedelta(hours=grace_hours)
    cutoff_dt = now - grace_td
    cutoff_ts = cutoff_dt.timestamp()
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
        'stalled_job': 0,
    }
    deferred_counts = {
        'missing_original': 0,
        'missing_preview': 0,
        'semantic_gap': 0,
        'ocr_gap': 0,
        'face_gap': 0,
    }
    truncated = {}

    def record_issue(kind, per_kind_cap=300, **detail):
        counts[kind] += 1
        if counts[kind] > per_kind_cap or len(issues) >= 300:
            truncated[kind] = True
        else:
            issues.append({'kind': kind, **detail})

    def in_grace(created_dt):
        return bool(grace_hours > 0 and created_dt and created_dt >= cutoff_dt)

    referenced_rels = set()
    active_photo_ids = set()
    active_sem_all = False
    active_ocr_all = False
    active_sem_datasets = set()
    active_ocr_datasets = set()

    stalled_td = timedelta(hours=max(1, grace_hours))
    for jb in BackgroundJob.objects.filter(state__in=('pending', 'running')).values(
        'id', 'kind', 'state', 'scope', 'params', 'lease_until', 'created', 'updated'
    ):
        is_stalled = (
            (jb['state'] == 'running' and jb['lease_until'] and jb['lease_until'] < now and jb['updated'] < now - stalled_td)
            or (jb['state'] == 'pending' and jb['created'] < now - stalled_td)
        )
        if is_stalled:
            record_issue('stalled_job', per_kind_cap=100, job_id=str(jb['id']), job_kind=jb['kind'], state=jb['state'])
            continue
        scope = jb['scope'] if isinstance(jb['scope'], dict) else {}
        params = jb['params'] if isinstance(jb['params'], dict) else {}
        for pid in _checked_rows(scope.get('photo_ids') or [], checkpoint):
            active_photo_ids.add(pid)
        if jb['kind'] == 'semantic_index':
            target = params.get('target', 'all')
            ds_list = scope.get('datasets') or []
            if not ds_list:
                if target in ('all', 'semantic'):
                    active_sem_all = True
                if target in ('all', 'ocr'):
                    active_ocr_all = True
            else:
                for did in _checked_rows(ds_list, checkpoint):
                    if target in ('all', 'semantic'):
                        active_sem_datasets.add(did)
                    if target in ('all', 'ocr'):
                        active_ocr_datasets.add(did)

    # 1, 2, 4: Photo file integrity checks
    for p in _checked_rows(Photo.objects.values('id', 'name', 'sha', 'status', 'dataset_id', 'file', 'preview', 'created').iterator(chunk_size=500), checkpoint):
        f_rel = _rel_norm(p['file'])
        p_rel = _rel_norm(p['preview'])
        if f_rel:
            referenced_rels.add(f_rel)
        if p_rel:
            referenced_rels.add(p_rel)

        active_media = in_grace(p['created']) and p['id'] in active_photo_ids
        orig_path = media_root / f_rel if f_rel else None
        if not f_rel or not orig_path or not orig_path.is_file():
            if active_media:
                deferred_counts['missing_original'] += 1
            else:
                record_issue('missing_original', photo_id=p['id'], name=p['name'], path=f_rel)
        elif verify_hashes and p['sha']:
            try:
                actual_sha = hashlib.sha256(orig_path.read_bytes()).hexdigest()
                if actual_sha != p['sha']:
                    record_issue('hash_mismatch', photo_id=p['id'], name=p['name'], expected=p['sha'], actual=actual_sha)
            except OSError:
                if active_media:
                    deferred_counts['missing_original'] += 1
                else:
                    record_issue('missing_original', photo_id=p['id'], name=p['name'], path=f_rel)

        prev_path = media_root / p_rel if p_rel else None
        if not p_rel or not prev_path or not prev_path.is_file():
            if active_media:
                deferred_counts['missing_preview'] += 1
            else:
                record_issue('missing_preview', photo_id=p['id'], name=p['name'], path=p_rel, reason='縮圖不存在')
        else:
            try:
                with Image.open(prev_path) as im:
                    im.verify()
            except (OSError, UnidentifiedImageError, ValueError):
                if active_media:
                    deferred_counts['missing_preview'] += 1
                else:
                    record_issue('missing_preview', photo_id=p['id'], name=p['name'], path=p_rel, reason='縮圖無法讀取')

    for rel in _checked_rows(Export.objects.values_list('file', flat=True), checkpoint):
        if rel:
            referenced_rels.add(_rel_norm(rel))
    for rel in _checked_rows(PersonReference.objects.values_list('file', flat=True), checkpoint):
        if rel:
            referenced_rels.add(_rel_norm(rel))

    # 3: Orphan files under MEDIA_ROOT (excluding protected control files, symlinks/junctions, and files within grace window)
    orphan_paths = {}
    if media_root.is_dir():
        for root, dirs, files in _checked_rows(os.walk(media_root, followlinks=False), checkpoint):
            dirs[:] = [
                d for d in dirs
                if not d.startswith('.') and not _is_symlink_or_junction(Path(root) / d)
            ]
            for fname in _checked_rows(files, checkpoint):
                full = Path(root) / fname
                if _is_symlink_or_junction(full):
                    continue
                rel = _rel_norm(full.relative_to(media_root))
                if is_protected_storage_rel(rel) or rel in referenced_rels:
                    continue
                try:
                    st = full.stat()
                except OSError:
                    continue
                if grace_hours > 0 and st.st_mtime >= cutoff_ts:
                    continue
                if len(orphan_paths) < 5000:
                    orphan_paths[rel] = _file_identity(full)
                record_issue('orphan_file', path=rel, size=st.st_size)

    # 5: Content (semantic + OCR) index gaps on submitted photos
    submitted_q = Photo.objects.filter(status='submitted')
    ready_sem = set(
        PhotoEmbedding.objects.filter(
            model_key=SEMANTIC_MODEL_KEY, state='ready', source_sha=F('photo__sha')
        ).values_list('photo_id', flat=True)
    )
    for row in _checked_rows(submitted_q.exclude(id__in=ready_sem).values('id', 'dataset_id', 'created').iterator(chunk_size=500), checkpoint):
        if in_grace(row['created']) and (
            row['id'] in active_photo_ids or active_sem_all or row['dataset_id'] in active_sem_datasets
        ):
            deferred_counts['semantic_gap'] += 1
        else:
            record_issue('semantic_gap', per_kind_cap=100, photo_id=row['id'], model=SEMANTIC_MODEL_KEY)

    ready_ocr = set(
        PhotoOcr.objects.filter(
            model_key=OCR_MODEL_KEY, state='ready', source_sha=F('photo__sha')
        ).values_list('photo_id', flat=True)
    )
    for row in _checked_rows(submitted_q.exclude(id__in=ready_ocr).values('id', 'dataset_id', 'created').iterator(chunk_size=500), checkpoint):
        if in_grace(row['created']) and (
            row['id'] in active_photo_ids or active_ocr_all or row['dataset_id'] in active_ocr_datasets
        ):
            deferred_counts['ocr_gap'] += 1
        else:
            record_issue('ocr_gap', per_kind_cap=100, photo_id=row['id'], model=OCR_MODEL_KEY)

    # 6: Face index failed/outdated checks (where face indexing has been run)
    stale_face_q = FacePhotoIndex.objects.exclude(state='ready', source_sha=F('photo__sha'))
    for row in _checked_rows(stale_face_q.values('photo_id', 'state', 'error', 'photo__created').iterator(chunk_size=500), checkpoint):
        if in_grace(row['photo__created']) and row['photo_id'] in active_photo_ids:
            deferred_counts['face_gap'] += 1
        else:
            record_issue('face_gap', per_kind_cap=100, photo_id=row['photo_id'], state=row['state'], error=row['error'])

    # 7: External gateway stalled receipts/grants
    from .models import ExternalImportReceipt
    stale_ext_q = ExternalImportReceipt.objects.filter(state='importing', updated__lt=now - stalled_td)
    for row in _checked_rows(stale_ext_q.values('id', 'grant_id', 'updated').iterator(chunk_size=500), checkpoint):
        record_issue('external_stale', per_kind_cap=100, receipt_id=str(row['id']), grant_id=str(row['grant_id']))

    summary = {
        'counts': counts,
        'total_issues': sum(counts.values()),
        'deferred_counts': deferred_counts,
        'total_deferred': sum(deferred_counts.values()),
        'truncated': truncated,
        'grace_hours': grace_hours,
        'orphan_paths': orphan_paths,
        'capacity': compute_capacity_report(checkpoint=checkpoint),
        'non_destructive': True,
    }
    if checkpoint:checkpoint()
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


@transaction.atomic
def confirm_orphan_cleanup(actor, paths, reason, *, report_id=None, grace_hours=24):
    """Delete explicitly confirmed report-bound orphan files only after live safety verification."""
    reason = str(reason or '').strip()
    if not reason:
        raise Problem('請填寫清理確認理由', 422)
    if not isinstance(paths, list) or not paths:
        raise Problem('請勾選要清理的孤立檔案路徑', 422)
    if not report_id:
        raise Problem('清理孤立檔必須綁定有效的對帳報告（缺少 report_id）', 422)
    try:
        report_uuid = uuid.UUID(str(report_id))
    except (ValueError, TypeError) as exc:
        raise Problem('對帳報告識別碼格式無效', 422) from exc
    report = StorageReconcileReport.objects.filter(id=report_uuid).first()
    if not report:
        raise Problem('找不到指定的對帳報告', 404)

    if connection.vendor == 'sqlite':
        StorageReconcileReport.objects.filter(id=report.id).update(mode=F('mode'))
    reported_orphans = report.summary.get('orphan_paths')
    if not isinstance(reported_orphans, dict):
        reported_orphans = {
            item['path']: {'size': item.get('size')}
            for item in (report.issues or [])
            if item.get('kind') == 'orphan_file' and item.get('path')
        }

    grace_hours = max(1, int(grace_hours))
    cutoff_ts = (timezone.now() - timedelta(hours=grace_hours)).timestamp()
    media_root = Path(settings.MEDIA_ROOT).resolve()

    # Pre-screen every requested path first so protected control files / traversals / links are strictly rejected
    normalized_targets = []
    for raw_rel in paths[:200]:
        raw_str = str(raw_rel or '')
        if not raw_str or raw_str.replace('\\', '/').startswith('/'):
            audit(actor, 'reconcile_orphan_cleanup_rejected', str(report.id), {'path': raw_str, 'reason': '絕對或空白路徑'})
            raise Problem('拒絕清理受保護檔案、控制檔或無效／越界路徑', 422)
        rel = _rel_norm(raw_str)
        resolved, err = _resolve_confined_path(media_root, rel)
        if err:
            audit(actor, 'reconcile_orphan_cleanup_rejected', str(report.id), {'path': raw_str, 'reason': err})
            raise Problem(f'拒絕清理（{err}）：{rel or raw_str}', 422)
        normalized_targets.append((rel, resolved))

    deleted = []
    skipped = []
    for rel, full in normalized_targets:
        if rel not in reported_orphans:
            skipped.append({'path': rel, 'reason': '檔案不在指定對帳報告的孤立檔清單中'})
            continue
        if _has_file_reference(rel):
            skipped.append({'path': rel, 'reason': '目前仍有資料庫引用或進行中工作關聯'})
            continue
        if _is_symlink_or_junction(full):
            skipped.append({'path': rel, 'reason': '拒絕刪除符號連結或目錄連結'})
            continue
        if not full.is_file():
            skipped.append({'path': rel, 'reason': '檔案已不存在'})
            continue
        try:
            st = full.lstat()
        except OSError:
            skipped.append({'path': rel, 'reason': '無法讀取檔案狀態'})
            continue
        if st.st_mtime >= cutoff_ts:
            skipped.append({'path': rel, 'reason': f'尚未超過 {grace_hours} 小時寬限期'})
            continue
        expected = reported_orphans[rel]
        if not isinstance(expected, dict) or not all(k in expected for k in ('size', 'mtime_ns', 'inode', 'device', 'sha256')):
            skipped.append({'path': rel, 'reason': '舊報告缺少完整檔案識別，請重新對帳'})
            continue
        try:current_identity = _file_identity(full)
        except OSError:
            skipped.append({'path': rel, 'reason': '檔案讀取失敗或已變動，請重新對帳'})
            continue
        if current_identity != expected:
            skipped.append({'path': rel, 'reason': '檔案已變更，請重新對帳'})
            continue
        # Atomically detach the verified object so unlink cannot target a replacement
        # that appears at the original name. Retain/restore it on any failed check.
        holding = media_root / '.cleanup-hold'
        holding.mkdir(exist_ok=True)
        if _is_symlink_or_junction(holding):raise Problem('清理暫存目錄不可為連結', 422)
        staged = holding / uuid.uuid4().hex
        try:full.rename(staged)
        except OSError:
            skipped.append({'path': rel, 'reason': '無法安全取得檔案，已取消清理'})
            continue
        try:
            _, path_error = _resolve_confined_path(media_root, rel)
            if path_error or _file_identity(staged) != expected or _has_file_reference(rel):
                skipped.append({'path': rel, 'reason': '刪除前引用或檔案狀態已變更，已取消清理'})
                continue
            staged.unlink()
        except OSError:
            skipped.append({'path': rel, 'reason': '檔案驗證或刪除失敗，已取消清理'})
            continue
        finally:
            if staged.exists():
                if os.name == 'nt':
                    try:staged.rename(full) # Windows rename refuses to replace a concurrent new file.
                    except OSError:
                        skipped.append({'path': rel, 'reason': '原位置已有變動，原檔已保留', 'retained_path': staged.relative_to(media_root).as_posix()})
                else:
                    try:os.link(staged, full);staged.unlink()
                    except OSError:
                        skipped.append({'path': rel, 'reason': '原檔已保留，需人工確認', 'retained_path': staged.relative_to(media_root).as_posix()})
            try:holding.rmdir()
            except OSError:pass

        deleted.append(rel)

    audit(
        actor,
        'reconcile_orphan_cleanup',
        len(deleted),
        {'report_id': str(report.id), 'deleted': deleted, 'skipped': skipped, 'reason': reason},
    )
    return {'report_id': str(report.id), 'deleted': deleted, 'skipped': skipped}
