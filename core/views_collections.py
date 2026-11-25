"""Saved searches and curated personal/department work collections."""
import uuid
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from .helpers import Problem, audit, dept_ids, manager, serialize_photos, visible, writable
from .models import Audit, Dataset, Department, Membership, Photo, SavedSearch, WorkCollection, WorkCollectionItem
from .search import SearchError, resolve_selection_ids, search_photos


def validate_saved_criteria(user, criteria):
    """Verify saved search departments, datasets, and custom fields still exist and remain authorized."""
    warnings = []
    if not isinstance(criteria, dict):
        return False, ['搜尋條件格式無效']

    allowed_deps = set(Department.objects.values_list('id', flat=True)) if user.is_superuser else set(dept_ids(user))
    req_deps = [int(x) for x in criteria.get('departments', []) if isinstance(x, int)]
    invalid_deps = [d for d in req_deps if d not in allowed_deps]
    if invalid_deps:
        warnings.append(f'部分科室已不存在或失去閱覽權限（ID：{", ".join(map(str, invalid_deps))}），不會自動放寬為全部搜尋')

    req_ds = [int(x) for x in criteria.get('datasets', []) if isinstance(x, int)]
    existing_ds = {
        d.id: d
        for d in Dataset.objects.select_related('item').filter(id__in=req_ds)
    }
    invalid_ds = [
        d for d in req_ds
        if d not in existing_ds or existing_ds[d].item.department_id not in allowed_deps
    ]
    if invalid_ds:
        warnings.append(f'部分資料集已失效或無權限（ID：{", ".join(map(str, invalid_ds))}），不會自動放寬為全部資料集')

    rules = criteria.get('fields', []) or []
    if rules:
        if len(req_ds) != 1 or req_deps or invalid_ds:
            warnings.append('自訂欄位篩選依賴的單一資料集已失效')
        else:
            ds = existing_ds[req_ds[0]]
            defs = {(f['key'], f.get('type', 'text')) for f in ds.item.fields}
            status = criteria.get('status', 'submitted')
            for snap in visible(user).filter(dataset_id=ds.id, status=status).values_list('form_snapshot', flat=True).iterator(chunk_size=200):
                for f in snap:
                    defs.add((f['key'], f.get('type', 'text')))
            bad_fields = [r.get('key') for r in rules if (r.get('key'), r.get('type')) not in defs]
            if bad_fields:
                warnings.append(f'部分自訂欄位已不屬於此資料集：{", ".join(map(str, bad_fields))}')

    return len(warnings) == 0, warnings


def serialize_saved_search(user, ss):
    valid, warnings = validate_saved_criteria(user, ss.criteria)
    return {
        'id': str(ss.id),
        'name': ss.name,
        'criteria': ss.criteria,
        'display_mode': ss.display_mode,
        'valid': valid,
        'warnings': warnings,
        'created': ss.created.isoformat(),
        'updated': ss.updated.isoformat(),
    }


def _visible_collections(user):
    if user.is_superuser:
        return WorkCollection.objects.select_related('owner', 'department').filter(
            Q(owner=user, department__isnull=True) | Q(department__isnull=False, shared=True) | Q(owner=user)
        )
    deps = dept_ids(user)
    return WorkCollection.objects.select_related('owner', 'department').filter(
        Q(owner=user, department__isnull=True) | Q(department_id__in=deps, shared=True)
    )


def _can_manage_collection(user, col):
    if col.department_id is None:
        return col.owner_id == user.id
    return writable(user, col.department_id) or manager(user, col.department_id)


def _serialize_collection(user, col):
    auth_count = WorkCollectionItem.objects.filter(
        collection=col,
        photo_id__in=visible(user).exclude(status='recycled').values('id'),
    ).count()
    return {
        'id': str(col.id),
        'name': col.name,
        'description': col.description,
        'owner': col.owner.username,
        'department': col.department_id,
        'department_name': col.department.name if col.department_id else '',
        'shared': col.shared,
        'can_manage': _can_manage_collection(user, col),
        'photo_count': auth_count,
        'created': col.created.isoformat(),
        'updated': col.updated.isoformat(),
    }


def _parse_uuid(value, label='識別碼'):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise Problem(f'{label}格式錯誤', 422) from exc


def handle_collections_api(path, data, u):
    if path == 'saved_searches/list':
        rows = SavedSearch.objects.filter(owner=u).order_by('-updated', '-created')
        return JsonResponse({'saved_searches': [serialize_saved_search(u, s) for s in rows]})

    if path == 'saved_searches/save':
        name = str(data.get('name') or '').strip()
        if not name or len(name) > 100:
            raise Problem('請輸入 1 至 100 字的常用搜尋名稱', 422)
        mode = str(data.get('display_mode') or 'map')
        if mode not in ('map', 'wall'):
            raise Problem('展示模式無效', 422)
        criteria = data.get('criteria')
        if not isinstance(criteria, dict):
            raise Problem('搜尋條件格式錯誤', 422)
        valid, warnings = validate_saved_criteria(u, criteria)
        if not valid:
            raise Problem('；'.join(warnings), 422)
        ss_id = data.get('id')
        if ss_id:
            sid = _parse_uuid(ss_id, '常用搜尋識別碼')
            ss = SavedSearch.objects.filter(id=sid, owner=u).first()
            if not ss:
                raise Problem('找不到常用搜尋或沒有權限', 404)
            ss.name = name
            ss.criteria = criteria
            ss.display_mode = mode
            ss.save()
        else:
            ss = SavedSearch.objects.create(owner=u, name=name, criteria=criteria, display_mode=mode)
        audit(u, 'saved_search_save', ss.id, {'name': ss.name})
        return JsonResponse({'ok': True, 'saved_search': serialize_saved_search(u, ss)})

    if path == 'saved_searches/delete':
        sid = _parse_uuid(data.get('id'), '常用搜尋識別碼')
        deleted, _ = SavedSearch.objects.filter(id=sid, owner=u).delete()
        if not deleted:
            raise Problem('找不到常用搜尋', 404)
        audit(u, 'saved_search_delete', sid)
        return JsonResponse({'ok': True})

    if path == 'saved_searches/apply':
        sid = _parse_uuid(data.get('id'), '常用搜尋識別碼')
        ss = SavedSearch.objects.filter(id=sid, owner=u).first()
        if not ss:
            raise Problem('找不到常用搜尋', 404)
        valid, warnings = validate_saved_criteria(u, ss.criteria)
        if not valid:
            return JsonResponse({
                'ok': False,
                'valid': False,
                'warnings': warnings,
                'saved_search': serialize_saved_search(u, ss),
            }, status=409)
        return JsonResponse({'ok': True, 'valid': True, 'warnings': [], 'saved_search': serialize_saved_search(u, ss)})

    if path == 'collections/list':
        cols = list(_visible_collections(u).order_by('-updated', '-created'))
        return JsonResponse({'collections': [_serialize_collection(u, c) for c in cols]})

    if path == 'collections/save':
        name = str(data.get('name') or '').strip()
        description = str(data.get('description') or '').strip()[:2000]
        if not name or len(name) > 120:
            raise Problem('請輸入 1 至 120 字的工作集合名稱', 422)
        dept_id = data.get('department')
        shared = bool(dept_id)
        dept = None
        if dept_id:
            dept = Department.objects.filter(id=int(dept_id)).first()
            if not dept:
                raise Problem('找不到指定科室', 404)
            if not (writable(u, dept.id) or manager(u, dept.id)):
                raise Problem('需要該科室整理或主管權限才可建立或管理科室共享集合', 403)

        cid = data.get('id')
        if cid:
            col_uuid = _parse_uuid(cid, '工作集合識別碼')
            col = WorkCollection.objects.select_for_update().select_related('department', 'owner').filter(id=col_uuid).first()
            if not col:
                raise Problem('找不到工作集合', 404)
            if not _can_manage_collection(u, col):
                raise Problem('沒有管理此工作集合的權限', 403)
            old_state = {'name': col.name, 'shared': col.shared, 'department': col.department_id}
            col.name = name
            col.description = description
            col.department = dept
            col.shared = shared
            col.save()
            audit(u, 'collection_save', col.id, {'before': old_state, 'after': {'name': name, 'shared': shared, 'department': col.department_id}})
        else:
            col = WorkCollection.objects.create(
                name=name,
                description=description,
                owner=u,
                department=dept,
                shared=shared,
            )
            audit(u, 'collection_save', col.id, {'created': True, 'name': name, 'shared': shared, 'department': col.department_id})
        return JsonResponse({'ok': True, 'collection': _serialize_collection(u, col)})

    if path == 'collections/delete':
        col_uuid = _parse_uuid(data.get('id'), '工作集合識別碼')
        col = WorkCollection.objects.filter(id=col_uuid).first()
        if not col:
            raise Problem('找不到工作集合', 404)
        if not _can_manage_collection(u, col):
            raise Problem('沒有刪除工作集合的權限', 403)
        name = col.name
        col.delete()
        audit(u, 'collection_delete', col_uuid, {'name': name})
        return JsonResponse({'ok': True})

    if path == 'collections/add_items':
        col_uuid = _parse_uuid(data.get('collection'), '工作集合識別碼')
        col = WorkCollection.objects.select_for_update().filter(id=col_uuid).first()
        if not col:
            raise Problem('找不到工作集合', 404)
        if not _can_manage_collection(u, col):
            raise Problem('沒有維護此工作集合的權限', 403)

        if data.get('selection_token'):
            ids = resolve_selection_ids(visible(u), u, data['selection_token'], data.get('excluded_photos'))
        else:
            raw = data.get('photos', [])
            if not isinstance(raw, list) or not raw or any(type(x) is not int for x in raw):
                raise Problem('請選擇要加入集合的照片', 422)
            ids = list(dict.fromkeys(raw))
        if len(ids) > 1000:
            raise Problem('單次加入工作集合上限 1000 張', 422)
        photos = list(visible(u).exclude(status='recycled').filter(id__in=ids))
        if len(photos) != len(ids):
            raise Problem('部分照片無權限或已移入回收區，無法加入集合', 409)
        existing = set(WorkCollectionItem.objects.filter(collection=col, photo_id__in=ids).values_list('photo_id', flat=True))
        to_create = [
            WorkCollectionItem(collection=col, photo=p, added_by=u)
            for p in photos
            if p.id not in existing
        ]
        if to_create:
            WorkCollectionItem.objects.bulk_create(to_create, ignore_conflicts=True)
            col.save(update_fields=['updated'])
        audit(u, 'collection_add', col.id, {'added': len(to_create), 'photo_ids': [x.photo_id for x in to_create[:100]]})
        return JsonResponse({'ok': True, 'added': len(to_create), 'collection': _serialize_collection(u, col)})

    if path == 'collections/remove_items':
        col_uuid = _parse_uuid(data.get('collection'), '工作集合識別碼')
        col = WorkCollection.objects.select_for_update().filter(id=col_uuid).first()
        if not col:
            raise Problem('找不到工作集合', 404)
        if not _can_manage_collection(u, col):
            raise Problem('沒有維護此工作集合的權限', 403)
        raw = data.get('photos', [])
        if not isinstance(raw, list) or not raw or any(type(x) is not int for x in raw):
            raise Problem('請選擇要移出集合的照片', 422)
        removed, _ = WorkCollectionItem.objects.filter(collection=col, photo_id__in=raw).delete()
        if removed:
            col.save(update_fields=['updated'])
        audit(u, 'collection_remove', col.id, {'removed': removed, 'photo_ids': raw[:100]})
        return JsonResponse({'ok': True, 'removed': removed, 'collection': _serialize_collection(u, col)})

    if path == 'collections/items':
        col_uuid = _parse_uuid(data.get('collection'), '工作集合識別碼')
        col = _visible_collections(u).filter(id=col_uuid).first()
        if not col:
            raise Problem('找不到工作集合或沒有閱覽權限', 404)
        try:
            search_res = search_photos(
                visible(u),
                u,
                {
                    'collection': str(col.id),
                    'status': data.get('status', 'submitted'),
                    'limit': data.get('limit', 100),
                    'offset': data.get('offset', 0),
                },
            )
        except SearchError as exc:
            raise Problem(str(exc), 422) from exc
        rows = search_res.pop('rows')
        search_res['photos'] = serialize_photos(rows, u)
        search_res['collection'] = _serialize_collection(u, col)
        events = list(
            Audit.objects.filter(target=str(col.id), action__startswith='collection_')
            .select_related('actor')
            .order_by('-id')
            .values('actor__username', 'action', 'detail', 'created')[:30]
        )
        search_res['events'] = [
            {
                'actor': e['actor__username'] or '系統',
                'action': e['action'],
                'detail': e['detail'],
                'created': e['created'].isoformat(),
            }
            for e in events
        ]
        return JsonResponse(search_res)

    raise Problem('找不到常用搜尋或工作集合功能', 404)
