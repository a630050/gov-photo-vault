import hashlib
import io
import uuid
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from .helpers import (
    Problem,
    audit,
    editable,
    manager,
    metadata,
    photo,
    photo_snapshot,
    update_photo_admin_region,
    validate,
    visible,
    writable,
)
from .image_metadata import extract_metadata
from .burst import assign_burst_group, compute_dhash_from_image, reconcile_burst_scope
from .models import Anomaly, Dataset, Membership, Notice, Photo, Request, RequestEvent, Star, TagAssertion
from .permissions import allowed
from .search import resolve_selection_ids


def _resolve_batch_photos(u, data, *, max_count=500, require_editable=False):
    if data.get('selection_token'):
        ids = resolve_selection_ids(visible(u), u, data['selection_token'], data.get('excluded_photos'))
        if not ids:
            raise Problem('目前條件下沒有可處理的照片', 409)
        if len(ids) > max_count:
            raise Problem(f'跨頁批次操作單次上限 {max_count} 張，目前選取 {len(ids)} 張，請縮小搜尋範圍', 422)
        photos = list(visible(u).select_for_update().filter(id__in=ids) if require_editable else visible(u).filter(id__in=ids))
        if len(photos) != len(ids):
            raise Problem('照片權限或狀態已變動', 409)
    else:
        ids = data.get('photos', [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= max_count or any(type(i) is not int for i in ids):
            raise Problem(f'請選擇 1 至 {max_count} 張照片', 422)
        unique_ids = set(ids)
        photos = list(visible(u).select_for_update().filter(id__in=unique_ids) if require_editable else visible(u).filter(id__in=unique_ids))
        if len(photos) != len(unique_ids):
            raise Problem('照片權限或狀態已變動', 409)
    if require_editable:
        for p in photos:
            editable(u, p)
    return photos


def _apply_tags(p, tags, batch, u):
    for tag in p.tags:
        if not TagAssertion.objects.filter(photo=p, tag=tag, active=True).exists():
            TagAssertion.objects.create(photo=p, tag=tag, batch='legacy', actor=p.uploader)
    for tag in set(tags):
        TagAssertion.objects.create(photo=p, tag=tag, batch=batch, actor=u)
    p.tags = sorted(set(p.tags + tags))
    p.save(update_fields=['tags'])


def handle_photo_api(request, path, data, u):
    if path == 'undo_tags':
        assertions = TagAssertion.objects.filter(batch=data['batch'], actor=u, active=True)
        pairs = list(assertions.values_list('photo_id', 'tag'))
        photos = list(visible(u).select_for_update().filter(id__in={pid for pid, _ in pairs}))
        if len(photos) != len({pid for pid, _ in pairs}):
            raise Problem('照片權限已改變，無法撤回本批次', 409)
        for p in photos:
            editable(u, p)
        assertions.update(active=False)
        for p in photos:
            tags = {tag for pid, tag in pairs if pid == p.id}
            retained = set(TagAssertion.objects.filter(photo=p, tag__in=tags, active=True).values_list('tag', flat=True))
            p.tags = [tag for tag in p.tags if tag not in tags or tag in retained]
            p.save(update_fields=['tags'])
        audit(u, path, data['batch'])
        return JsonResponse({'ok': True})

    if path == 'upload':
        ds = Dataset.objects.select_related('item').get(id=request.POST['dataset'])
        if not allowed(u, ds.item.department_id, 'upload'):
            raise Problem('沒有上傳權限', 403)
        if ds.archived or not ds.item.active:
            raise Problem('目的地不可上傳', 409)
        expected = request.POST.get('form_version')
        if expected and (int(expected) != ds.item.version or int(request.POST.get('form_id', 0)) != ds.item_id):
            raise Problem('資料集指定表單已更新，請重新確認表單後上傳', 409)
        files = request.FILES.getlist('files')
        if not files or len(files) > 500:
            raise Problem('每批需 1 至 500 張')
        results = []
        for f in files:
            if f.size > 50 * 1024 * 1024:
                results.append({'name': f.name, 'error': '超過 50 MB'})
                continue
            raw = f.read()
            sha = hashlib.sha256(raw).hexdigest()
            if Photo.objects.filter(dataset=ds, sha=sha).exists():
                results.append({'name': f.name, 'error': '此資料集已有相同檔案'})
                continue
            try:
                im = Image.open(io.BytesIO(raw))
                im.load()
                if im.format not in ['JPEG', 'PNG', 'WEBP', 'HEIF', 'HEIC']:
                    raise ValueError('不支援的圖片格式')
                cap, time_confirmed, lat, lon, extracted = extract_metadata(im)
                im = ImageOps.exif_transpose(im).convert('RGB')
                dhash = compute_dhash_from_image(im)
                im.thumbnail((1800, 1800))
                out = io.BytesIO()
                im.save(out, 'JPEG', quality=88)
                p = Photo(
                    dataset=ds,
                    uploader=u,
                    name=Path(f.name).name,
                    sha=sha,
                    dhash=dhash,
                    form_snapshot=ds.item.fields,
                    form_version=ds.item.version,
                    captured=cap,
                    time_confirmed=time_confirmed,
                    lat=lat,
                    lon=lon,
                    extracted=extracted,
                )
                p.file.save(uuid.uuid4().hex + Path(f.name).suffix.lower(), ContentFile(raw), save=False)
                p.preview.save(uuid.uuid4().hex + '.jpg', ContentFile(out.getvalue()), save=False)
                update_photo_admin_region(p)
                p.save()
                audit(u, 'upload', p.id)
                results.append({'name': p.name, 'id': p.id})
            except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError):
                results.append({'name': f.name, 'error': '無法解碼或不支援格式'})
        return JsonResponse({'results': results})

    if path == 'batch_star':
        photos = _resolve_batch_photos(u, data, max_count=500, require_editable=False)
        existing = set(Star.objects.filter(user=u, photo__in=photos).values_list('photo_id', flat=True))
        to_create = [Star(user=u, photo=p) for p in photos if p.id not in existing]
        Star.objects.bulk_create(to_create)
        for p in photos:
            if p.id not in existing:
                audit(u, 'star', p.id)
        return JsonResponse({'ok': True, 'count': len(to_create), 'matched': len(photos)})

    if path == 'batch_tags':
        tags = [str(t).strip()[:60] for t in data.get('tags', []) if str(t).strip()]
        if not tags:
            raise Problem('請輸入至少一個標籤', 422)
        photos = _resolve_batch_photos(u, data, max_count=500, require_editable=True)
        batch = data.get('batch') or str(uuid.uuid4())
        for p in photos:
            _apply_tags(p, tags, batch, u)
            audit(u, 'tags', p.id, {'batch': batch, 'tags': tags})
        return JsonResponse({'ok': True, 'batch': batch, 'count': len(photos)})

    if path in ['batch_save', 'batch_submit']:
        items = data.get('items', [])
        if not isinstance(items, list) or not 1 <= len(items) <= 500:
            raise Problem('單次批次處理需 1 至 500 張照片', 422)
        submit = path == 'batch_submit'
        results = []
        for entry in items:
            pid = entry.get('photo')
            name = f'#{pid}'
            try:
                with transaction.atomic():
                    p = visible(u).select_for_update().filter(id=pid).first()
                    if not p:
                        raise Problem('找不到照片或沒有權限', 404)
                    name = p.name
                    editable(u, p, 'upload')
                    if p.status != 'draft' or (p.uploader != u and not manager(u, p.dataset.item.department_id)):
                        raise Problem('無法編輯此草稿', 403)
                    if entry.get('version') != p.version:
                        raise Problem('資料已變動，請重新載入', 409)
                    for k, v in metadata(entry).items():
                        setattr(p, k, v)
                    update_photo_admin_region(p)
                    if submit:
                        validate(p)
                        p.status = 'submitted'
                    p.version += 1
                    p.save()
                    if submit:
                        assign_burst_group(p, actor=u)
                    audit(u, 'submit' if submit else 'save', p.id)
                results.append({'id': p.id, 'name': name, 'ok': True})
            except Problem as e:
                results.append({'id': pid, 'name': name, 'error': e.message})
            except (ValueError, KeyError, TypeError):
                results.append({'id': pid, 'name': name, 'error': '欄位格式錯誤或缺少必要資料'})
        return JsonResponse({'results': results})

    if path in ['save', 'submit', 'star', 'tags', 'request', 'restore', 'anomaly', 'discard']:
        p = photo(u, data['photo'])
        p = Photo.objects.select_for_update().select_related('dataset__item', 'uploader').get(id=p.id)
        if path == 'star':
            s = Star.objects.filter(user=u, photo=p)
            if s.exists():
                s.delete()
            else:
                Star.objects.create(user=u, photo=p)
        elif path == 'restore':
            if not manager(u, p.dataset.item.department_id):
                raise Problem('需要主管權限', 403)
            if p.status != 'recycled':
                raise Problem('照片未回收', 409)
            p.status = p.previous_status
            p.version += 1
            p.save()
            if p.status == 'submitted':
                reconcile_burst_scope(p.dataset_id, time_anchors=[p.captured], actor=u, reason='restored')
        else:
            if path == 'request':
                if not writable(u, p.dataset.item.department_id):
                    raise Problem('沒有整理權限', 403)
                if p.status != 'submitted':
                    raise Problem('只可申請正式照片', 409)
            else:
                editable(u, p, 'upload' if path in ['save', 'submit', 'discard'] else 'organize')
            if path in ['save', 'submit', 'discard']:
                if p.status != 'draft' or (p.uploader != u and not manager(u, p.dataset.item.department_id)):
                    raise Problem('無法編輯此草稿', 403)
                if data.get('version') != p.version:
                    raise Problem('資料已變動，請重新載入', 409)
                if path == 'discard':
                    old_group = p.burst_group
                    old_cap = p.captured
                    p.previous_status = 'draft'
                    p.status = 'recycled'
                    p.burst_group = ''
                    p.burst_cover = False
                    p.burst_cover_at = None
                else:
                    for k, v in metadata(data).items():
                        setattr(p, k, v)
                    update_photo_admin_region(p)
                    if path == 'submit':
                        validate(p)
                        p.status = 'submitted'
                p.version += 1
                p.save()
                if path == 'submit':
                    assign_burst_group(p, actor=u)
                elif path == 'discard' and old_group:
                    reconcile_burst_scope(
                        p.dataset_id,
                        time_anchors=[old_cap],
                        old_group_ids=[old_group],
                        actor=u,
                        reason='recycled',
                    )
            elif path == 'tags':
                tags = [str(t).strip()[:60] for t in data.get('tags', []) if str(t).strip()]
                batch = data.get('batch') or str(uuid.uuid4())
                _apply_tags(p, tags, batch, u)
                audit(u, path, p.id, {'batch': batch, 'tags': tags})
                return JsonResponse({'ok': True, 'batch': batch})
            elif path == 'request':
                kind = data['kind']
                if kind not in ['delete', 'correct', 'move']:
                    raise Problem('未知申請類型')
                if not data.get('reason', '').strip():
                    raise Problem('請填理由')
                if data.get('version') is not None and data['version'] != p.version:
                    raise Problem('照片已變動，請重新檢查', 409)
                proposal = data.get('payload', {})
                before = photo_snapshot(p)
                if kind == 'correct':
                    combined = {**before, **proposal}
                    values = metadata(combined)
                    probe = Photo(form_snapshot=p.form_snapshot, attrs=values['attrs'])
                    validate(probe)
                    proposal = {**values, 'captured': values['captured'].isoformat() if values['captured'] else None}
                    if Request.objects.filter(photo=p, kind='correct', state='pending').exists():
                        raise Problem('此照片已有待審更正，請先處理或撤回', 409)
                if kind == 'delete' and Request.objects.filter(photo=p, kind='delete', state='pending').exists():
                    raise Problem('此照片已有待審刪除申請', 409)
                r = Request.objects.create(
                    photo=p,
                    author=u,
                    kind=kind,
                    reason=data['reason'],
                    payload=proposal,
                    before=before,
                    base_version=p.version,
                )
                RequestEvent.objects.create(
                    request=r,
                    actor=u,
                    action='submitted',
                    detail={'reason': r.reason, 'before': before, 'proposed': proposal},
                )
                recipients = set(
                    Membership.objects.filter(
                        department_id=p.dataset.item.department_id, role='supervisor', user__is_active=True
                    ).values_list('user_id', flat=True)
                ) | set(User.objects.filter(is_superuser=True, is_active=True).values_list('id', flat=True))
                for recipient in recipients - {u.id}:
                    Notice.objects.create(user_id=recipient, text=f'照片申請 #{r.id} 待處理（{kind}）')
            elif path == 'anomaly':
                if not data.get('text', '').strip():
                    raise Problem('請填異常類型與說明')
                Anomaly.objects.create(photo=p, author=u, text=data['text'])
        audit(u, path, p.id)
        return JsonResponse({'ok': True})

    if path == 'burst_cover':
        p = photo(u, data['photo'])
        Dataset.objects.select_for_update().get(id=p.dataset_id)
        p = Photo.objects.select_for_update().select_related('dataset__item').get(id=p.id)
        editable(u, p, 'organize')
        if p.status != 'submitted' or not p.burst_group:
            raise Problem('此照片不屬於已提交的連拍組', 409)
        now = timezone.now()
        Photo.objects.select_for_update().filter(
            dataset_id=p.dataset_id, burst_group=p.burst_group
        ).exclude(id=p.id).update(burst_cover=False, burst_cover_at=None)
        p.burst_cover = True
        p.burst_cover_at = now
        p.save(update_fields=['burst_cover', 'burst_cover_at'])
        audit(u, 'burst_cover', p.id, {'burst_group': p.burst_group, 'algo': p.burst_algo})
        return JsonResponse({'ok': True, 'burst_group': p.burst_group})

    if path == 'burst_recompute':
        if not u.is_superuser:
            raise Problem('僅系統管理者可重新計算連拍群組', 403)
        ds_id = data.get('dataset')
        if ds_id:
            ds = Dataset.objects.filter(id=int(ds_id)).first()
            if not ds:
                raise Problem('找不到資料集', 404)
            stats = reconcile_burst_scope(ds.id, actor=u, reason='recompute', recompute_all=True)
            return JsonResponse({'ok': True, **stats})
        total_processed = 0
        total_groups = 0
        for ds in Dataset.objects.all().order_by('id'):
            st = reconcile_burst_scope(ds.id, actor=u, reason='recompute', recompute_all=True)
            total_processed += st['processed']
            total_groups += st['groups']
        return JsonResponse({'ok': True, 'processed': total_processed, 'groups': total_groups})

    return None
