import csv
import io
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from django.core.files import File
from django.http import FileResponse, JsonResponse
from django.utils import timezone
from .helpers import (
    Problem,
    audit,
    manager,
    metadata,
    photo_snapshot,
    request_scope,
    revision,
    update_photo_admin_region,
    validate,
    visible,
)
from .models import Anomaly, Dataset, Export, Notice, Photo, Request, RequestEvent
from .permissions import allowed
from .burst import reconcile_burst_scope


def cleanup_expired_exports():
    cutoff = timezone.now() - timedelta(days=7)
    for expired in Export.objects.filter(created__lt=cutoff):
        if expired.file:
            expired.file.delete(save=False)
        expired.delete()


def handle_workflow_api(path, data, u):
    if path == 'decision':
        r = request_scope(u).select_for_update().select_related('photo__dataset__item', 'author').get(id=data['id'])
        p = r.photo
        if data.get('decision') not in ['withdraw', 'approve', 'reject']:
            raise Problem('未知審核動作')
        if data.get('decision') == 'withdraw':
            if r.author != u:
                raise Problem('只能撤回本人申請', 403)
            state = 'withdrawn'
        else:
            if not manager(u, p.dataset.item.department_id):
                raise Problem('需要主管權限', 403)
            if r.author == u and not u.is_superuser:
                raise Problem('本人申請需由他人核准', 403)
            state = 'approved' if data.get('decision') == 'approve' else 'rejected'
            if not data.get('reason', '').strip():
                raise Problem('請填處理理由')
        if r.state != 'pending':
            raise Problem('案件已結案', 409)
        if state == 'approved':
            p = Photo.objects.select_for_update().select_related('dataset__item').get(id=p.id)
            if p.version != r.base_version or p.status != 'submitted':
                raise Problem('照片已變動，請駁回並重新申請', 409)
            revision(p, u)
            old_dataset_id = p.dataset_id
            old_group = p.burst_group
            old_captured = p.captured
            if r.kind == 'delete':
                p.previous_status = p.status
                p.status = 'recycled'
                p.burst_group = ''
                p.burst_cover = False
                p.burst_cover_at = None
                for other in Request.objects.filter(photo=p, state='pending').exclude(id=r.id):
                    other.state = 'invalidated'
                    other.closed_at = timezone.now()
                    other.decision_reason = '照片已回收，原申請失效'
                    other.save()
                    RequestEvent.objects.create(
                        request=other,
                        actor=u,
                        action='invalidated',
                        detail={'reason': other.decision_reason, 'caused_by_request': r.id},
                    )
                    Notice.objects.create(user=other.author, text=f'申請 #{other.id} 因照片回收而失效')
            elif r.kind == 'correct':
                for k, v in metadata(r.payload).items():
                    setattr(p, k, v)
                update_photo_admin_region(p)
                validate(p)
            elif r.kind == 'move':
                dest = Dataset.objects.get(id=r.payload['dataset'])
                if dest.item_id != p.dataset.item_id or dest.archived:
                    raise Problem('只能移至同項目未封存資料集')
                p.dataset = dest
                p.burst_group = ''
                p.burst_cover = False
                p.burst_cover_at = None
            p.version += 1
            p.save()
            if r.kind == 'delete' and old_group:
                reconcile_burst_scope(
                    old_dataset_id,
                    time_anchors=[old_captured],
                    old_group_ids=[old_group],
                    actor=u,
                    reason='recycled',
                )
            elif r.kind == 'correct' and (old_captured != p.captured or old_group):
                reconcile_burst_scope(
                    p.dataset_id,
                    time_anchors=[old_captured, p.captured],
                    old_group_ids=[old_group],
                    actor=u,
                    reason='correct',
                )
            elif r.kind == 'move':
                if old_group:
                    reconcile_burst_scope(
                        old_dataset_id,
                        time_anchors=[old_captured],
                        old_group_ids=[old_group],
                        actor=u,
                        reason='move_out',
                    )
                reconcile_burst_scope(
                    p.dataset_id,
                    time_anchors=[p.captured],
                    actor=u,
                    reason='move_in',
                )
        r.state = state
        r.reviewer = u
        r.decision_reason = data.get('reason', '')
        r.closed_at = timezone.now()
        r.save()
        RequestEvent.objects.create(
            request=r, actor=u, action=state, detail={'reason': r.decision_reason, 'after': photo_snapshot(p)}
        )
        Notice.objects.create(user=r.author, text=f'申請 #{r.id} 已處理：{state}')
        audit(
            u,
            'decision',
            r.id,
            {'state': state, 'reason': r.decision_reason, 'before': r.before, 'after': photo_snapshot(p)},
        )
        return JsonResponse({'ok': True})

    if path == 'resolve':
        a = Anomaly.objects.select_related('photo__dataset__item').get(id=data['id'])
        if not manager(u, a.photo.dataset.item.department_id):
            raise Problem('需要主管權限', 403)
        if data['state'] not in ['pending', 'confirmed', 'dismissed'] or not data.get('reason'):
            raise Problem('需填處理理由')
        a.state = data['state']
        a.resolution = data['reason']
        a.save()
        audit(u, path, a.id, {'state': a.state, 'reason': a.resolution})
        return JsonResponse({'ok': True})

    if path == 'notice':
        Notice.objects.filter(user=u, id=data['id']).update(read=True)
        return JsonResponse({'ok': True})

    if path == 'export':
        ids = list(set(map(int, data['photos'])))
        if not ids or len(ids) > 500:
            raise Problem('單次匯出需 1 至 500 張')
        photos = list(visible(u).filter(id__in=ids, status='submitted'))
        if len(photos) != len(ids):
            raise Problem('照片狀態或權限已變動', 409)
        if any(
            not allowed(u, p.dataset.item.department_id, 'export')
            or not allowed(u, p.dataset.item.department_id, 'download')
            for p in photos
        ):
            raise Problem('需要批次匯出及原圖下載權限', 403)

        cleanup_expired_exports()

        csvout = io.StringIO()
        writer = csv.writer(csvout)
        writer.writerow(['id', 'name', 'dataset', 'captured', 'lat', 'lon', 'tags', 'attributes'])

        def safe(value):
            text = str(value)
            return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
                tmp_path = tmp.name
            with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for p in photos:
                    arcname = f'photos/{p.id}{Path(p.name).suffix}'
                    with p.file.open('rb') as src, z.open(arcname, 'w') as dst:
                        shutil.copyfileobj(src, dst, length=65536)
                    writer.writerow([
                        p.id,
                        safe(p.name),
                        safe(p.dataset.name),
                        p.captured.isoformat() if p.captured else '',
                        p.lat,
                        p.lon,
                        safe(','.join(p.tags)),
                        safe(json.dumps(p.attrs, ensure_ascii=False)),
                    ])
                z.writestr('metadata.csv', csvout.getvalue().encode('utf-8-sig'))
            e = Export(actor=u, photo_ids=ids)
            with open(tmp_path, 'rb') as fp:
                e.file.save(str(uuid.uuid4()) + '.zip', File(fp))
            audit(u, path, e.id, {'photos': ids})
            return JsonResponse({'ok': True, 'url': f'/exports/{e.id}'})
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return None


def download_export(request, id):
    if not request.user.is_authenticated:
        return JsonResponse({'error': '請登入'}, status=401)
    e = Export.objects.filter(id=id, actor=request.user).first()
    if not e:
        return JsonResponse({'error': '沒有權限'}, status=403)
    if timezone.now() - e.created > timedelta(days=7):
        if e.file:
            e.file.delete(save=False)
        e.delete()
        return JsonResponse({'error': '下載包已到期'}, status=410)
    total_visible = 0
    for start in range(0, len(e.photo_ids), 500):
        chunk = e.photo_ids[start : start + 500]
        if any(
            not allowed(request.user, p.dataset.item.department_id, 'export')
            or not allowed(request.user, p.dataset.item.department_id, 'download')
            for p in Photo.objects.filter(id__in=chunk).select_related('dataset__item')
        ):
            return JsonResponse({'error': '匯出或下載權限已撤銷'}, status=403)
        total_visible += visible(request.user).filter(id__in=chunk, status='submitted').count()
    if total_visible != len(e.photo_ids):
        return JsonResponse({'error': '照片已變動，請重新匯出'}, status=409)
    audit(request.user, 'export_download', e.id)
    response = FileResponse(e.file.open('rb'), as_attachment=True, filename=f'photos-{e.id}.zip')
    response['Cache-Control'] = 'private, no-store'
    return response
