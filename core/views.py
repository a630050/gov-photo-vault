import json
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count, Q
from django.http import FileResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from .comparison import nearby_photos
from .helpers import (  # noqa: F401 - re-exported for backward compatibility
    FIELD_TYPES,
    Problem,
    audit,
    dept_ids,
    dt,
    editable,
    form_fields,
    manager,
    metadata,
    photo,
    photo_snapshot,
    request_scope,
    revision,
    serialize,
    serialize_photos,
    serialize_request,
    serialize_requests,
    validate,
    visible,
    writable,
)
from .models import (
    Anomaly,
    Audit,
    BackgroundJob,
    Config,
    Dataset,
    Department,
    Item,
    Notice,
    Photo,
    Request,
)
from .permissions import allowed, capabilities_data, memberships_data
from .search import (
    SearchError,
    authorized_filter_options,
    cluster_map_photos,
    search_photos,
)
from .views_admin import handle_admin_api
from .views_photo import handle_photo_api
from .views_workflow import download_export, handle_workflow_api  # noqa: F401


@ensure_csrf_cookie
def home(request):
    from django.conf import settings
    return render(request, 'index.html', {'csrf_cookie_name': settings.CSRF_COOKIE_NAME})


def asset(request, id, variant):
    try:
        if not request.user.is_authenticated:
            raise Problem('請先登入', 401)
        p = photo(request.user, id)
        if variant not in ['original', 'preview']:
            raise Problem('找不到檔案', 404)
        if variant == 'original' and not allowed(request.user, p.dataset.item.department_id, 'download'):
            raise Problem('沒有原圖下載權限', 403)
        field = p.preview if variant == 'preview' else p.file
        if not field:
            raise Problem('預覽未完成', 404)
        response = FileResponse(
            field.open('rb'),
            as_attachment=variant == 'original',
            filename=p.name if variant == 'original' else 'preview.jpg',
        )
        response['Cache-Control'] = 'private, no-store'
        if variant == 'original':
            audit(request.user, 'download', p.id)
        return response
    except Problem as e:
        return JsonResponse({'error': e.message}, status=e.status)
    except OSError:
        return JsonResponse({'error': '圖片暫不可用'}, status=503)


def api(request, path):
    try:
        data = json.loads(request.body or '{}') if request.content_type == 'application/json' else {}
        u = request.user
        method = request.method
        if path == 'login' and method == 'POST':
            user = authenticate(request, username=data.get('username'), password=data.get('password'))
            if not user:
                raise Problem('帳號或密碼錯誤', 401)
            login(request, user)
            audit(user, 'login', user.id)
            return JsonResponse({'ok': True})
        if not u.is_authenticated:
            raise Problem('請先登入', 401)
        if path.startswith('external/') and method == 'POST':
            from .external_upload import external_api
            return external_api(path, data, u)
        if path.startswith('people/') and method == 'POST':
            from .people import people_api
            return people_api(request, path, data, u)
        if path.startswith('semantic/') and method == 'POST':
            from .semantic import semantic_api
            return semantic_api(path, data, u)
        if path.startswith('similarity/') and method == 'POST':
            from .similarity import similarity_api
            return similarity_api(path, data, u)
        if path.startswith('jobs/') and method == 'POST':
            from .views_jobs import handle_jobs_api
            return handle_jobs_api(path, data, u)
        if path.startswith(('saved_searches/', 'collections/')) and method == 'POST':
            from .views_collections import handle_collections_api
            return handle_collections_api(path, data, u)
        if path.startswith('reconcile/') and method == 'POST':
            from .views_reconcile import handle_reconcile_api
            return handle_reconcile_api(path, data, u)
        if path.startswith('integration_admin/') and method == 'POST':
            from .views_integration import handle_integration_admin_api
            with transaction.atomic():
                res = handle_integration_admin_api(path, data, u)
                if res is not None:
                    return res
        if path == 'nearby' and method == 'POST':
            try:
                with transaction.atomic():
                    base = photo(u, data['photo'])
                    result = nearby_photos(visible(u), u, base, data)
                    rows = result.pop('rows')
                    serialized = {item['id']: item for item in serialize_photos([base] + [p for p, _ in rows], u)}
                    result['candidates'] = [{**serialized[p.id], 'distance_m': round(distance, 2)} for p, distance in rows]
                    result['base'] = serialized[base.id]
                    return JsonResponse(result)
            except SearchError as exc:
                raise Problem(str(exc), 422)
        if path == 'search' and method == 'POST':
            try:
                with transaction.atomic():
                    result = search_photos(visible(u), u, data)
                    rows = result.pop('rows')
                    result['photos'] = serialize_photos(rows, u)
                    return JsonResponse(result)
            except SearchError as exc:
                raise Problem(str(exc), 422)
        if path == 'map_clusters' and method == 'POST':
            try:
                return JsonResponse(cluster_map_photos(visible(u), u, data))
            except SearchError as exc:
                raise Problem(str(exc), 422)
        if path == 'filter_options' and method == 'POST':
            try:
                return JsonResponse(authorized_filter_options(visible(u), u, data))
            except SearchError as exc:
                raise Problem(str(exc), 422)
        if path == 'photo_detail' and method == 'POST':
            p = photo(u, int(data['photo']))
            return JsonResponse({'photo': serialize_photos([p], u)[0]})
        if path == 'requests' and method == 'POST':
            limit = int(data.get('limit', 50))
            offset = int(data.get('offset', 0))
            if not 1 <= limit <= 200 or offset < 0:
                raise Problem('分頁參數無效', 422)
            req_qs = request_scope(u).select_related('photo__dataset__item', 'author', 'reviewer').prefetch_related('events__actor')
            managed = dept_ids(u, True)
            todo_q = req_qs.filter(state='pending') if u.is_superuser else req_qs.filter(state='pending', photo__dataset__item__department_id__in=managed).exclude(author=u)
            todo_count = todo_q.count()
            req_id = data.get('id')
            if req_id is not None:
                filtered = req_qs.filter(id=int(req_id))
            else:
                view = data.get('view', 'history')
                if view == 'todo':
                    filtered = todo_q
                elif view == 'mine':
                    filtered = req_qs.filter(author=u)
                elif view == 'history':
                    filtered = req_qs
                else:
                    raise Problem('未知檢視類型', 422)
                if data.get('department'):
                    filtered = filtered.filter(photo__dataset__item__department_id=int(data['department']))
                if data.get('state'):
                    filtered = filtered.filter(state=str(data['state']))
                if data.get('kind'):
                    filtered = filtered.filter(kind=str(data['kind']))
            total = filtered.count()
            page_rows = list(filtered.order_by('-id')[offset : offset + limit])
            has_next = (offset + len(page_rows)) < total
            return JsonResponse({
                'requests': serialize_requests(page_rows, u),
                'total': total,
                'todo_count': todo_count,
                'offset': offset,
                'limit': limit,
                'has_next': has_next,
                'next_offset': (offset + len(page_rows)) if has_next else None,
            })
        if path == 'notices' and method == 'POST':
            limit = int(data.get('limit', 50))
            offset = int(data.get('offset', 0))
            if not 1 <= limit <= 200 or offset < 0:
                raise Problem('分頁參數無效', 422)
            base_q = Notice.objects.filter(user=u)
            unread_count = base_q.filter(read=False).count()
            q = base_q.filter(read=False) if data.get('unread_only') else base_q
            total = q.count()
            rows = list(q.order_by('-id').values()[offset : offset + limit])
            has_next = (offset + len(rows)) < total
            return JsonResponse({
                'notices': rows,
                'total': total,
                'unread_count': unread_count,
                'offset': offset,
                'limit': limit,
                'has_next': has_next,
                'next_offset': (offset + len(rows)) if has_next else None,
            })
        if path == 'logout' and method == 'POST':
            logout(request)
            return JsonResponse({'ok': True})
        if path == 'state' and method == 'GET':
            deps = Department.objects.all() if u.is_superuser else Department.objects.filter(id__in=dept_ids(u))
            ids = list(deps.values_list('id', flat=True))
            managed = dept_ids(u, True)
            vis = visible(u)
            dataset_counts = {
                str(r['dataset_id']): r['count']
                for r in vis.values('dataset_id').annotate(count=Count('id'))
            }
            draft_counts = {
                str(r['dataset_id']): r['count']
                for r in vis.filter(status='draft').values('dataset_id').annotate(count=Count('id'))
            }
            req_base = request_scope(u).filter(state='pending')
            todo_requests = (
                req_base.count()
                if u.is_superuser
                else req_base.filter(photo__dataset__item__department_id__in=managed).exclude(author=u).count()
            )
            unread_notices = Notice.objects.filter(user=u, read=False).count()
            active_jobs = BackgroundJob.objects.filter(actor=u, state__in=['pending', 'running']).count()
            cfg = Config.objects.get_or_create(id=1)[0]
            return JsonResponse(dict(
                user={
                    'name': u.username,
                    'admin': u.is_superuser,
                    'managed': managed,
                    'writable': [d for d in ids if allowed(u, d, 'upload')],
                    'capabilities': capabilities_data(u),
                },
                departments=list(deps.values()),
                items=list(Item.objects.filter(department_id__in=ids).values()),
                datasets=list(Dataset.objects.filter(item__department_id__in=ids).values()),
                summary={
                    'visible_photos': sum(dataset_counts.values()),
                    'dataset_counts': dataset_counts,
                    'draft_counts': draft_counts,
                    'todo_requests': todo_requests,
                    'unread_notices': unread_notices,
                    'active_jobs': active_jobs,
                },
                filter_options=authorized_filter_options(vis, u, {'status': 'submitted'}),
                endpoints={
                    'photos': '/api/search',
                    'photo_detail': '/api/photo_detail',
                    'map_clusters': '/api/map_clusters',
                    'filter_options': '/api/filter_options',
                    'requests': '/api/requests',
                    'notices': '/api/notices',
                    'jobs': '/api/jobs/list',
                },
                radius=cfg.radius,
                burst_config={
                    'gap_seconds': cfg.burst_gap_seconds,
                    'dhash_max': cfg.burst_dhash_max,
                    'span_seconds': cfg.burst_span_seconds,
                },
                users=list(User.objects.values('id', 'username', 'is_active', 'is_superuser')) if u.is_superuser else [],
                memberships=memberships_data() if u.is_superuser else [],
                audits=(
                    list(
                        Audit.objects.exclude(Q(action__startswith='person_') | Q(action__startswith='face_'))
                        .order_by('-id')
                        .values('id', 'actor__username', 'action', 'target', 'detail', 'created')[:100]
                    )
                    if u.is_superuser
                    else []
                ),
            ))
        if method != 'POST':
            raise Problem('不支援的方法', 405)

        with transaction.atomic():
            for handler in (
                lambda: handle_admin_api(path, data, u),
                lambda: handle_workflow_api(path, data, u),
                lambda: handle_photo_api(request, path, data, u),
            ):
                response = handler()
                if response is not None:
                    return response
            raise Problem('找不到功能', 404)
    except Problem as e:
        return JsonResponse({'error': e.message}, status=e.status)
    except IntegrityError:
        return JsonResponse({'error': '名稱或帳號重複，請重新確認'}, status=409)
    except OperationalError:
        return JsonResponse({'error': '資料正在被其他操作更新或暫時不可用，請重新整理後再試'}, status=409)
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': '欄位格式錯誤或缺少必要資料'}, status=422)
    except (
        Photo.DoesNotExist,
        Item.DoesNotExist,
        Dataset.DoesNotExist,
        Request.DoesNotExist,
        Anomaly.DoesNotExist,
        User.DoesNotExist,
        Department.DoesNotExist,
    ):
        return JsonResponse({'error': '找不到資料'}, status=404)
