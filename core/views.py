import json
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q
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
    Config,
    Dataset,
    Department,
    Item,
    Notice,
    Photo,
    Request,
)
from .permissions import allowed, capabilities_data, memberships_data
from .search import SearchError, search_photos
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
                    rows = {p.id: p for p in visible(u).filter(id__in=result['ids'])}
                    ordered = [rows[id] for id in result['ids'] if id in rows]
                    result['photos'] = serialize_photos(ordered, u)
                    return JsonResponse(result)
            except SearchError as exc:
                raise Problem(str(exc), 422)
        if path == 'logout' and method == 'POST':
            logout(request)
            return JsonResponse({'ok': True})
        if path == 'state' and method == 'GET':
            deps = Department.objects.all() if u.is_superuser else Department.objects.filter(id__in=dept_ids(u))
            ids = list(deps.values_list('id', flat=True))
            photos = visible(u).order_by('-id')
            requests_qs = (
                request_scope(u)
                .select_related('photo__dataset__item', 'author', 'reviewer')
                .prefetch_related('events__actor')
                .order_by('-id')
            )
            return JsonResponse(dict(
                user={
                    'name': u.username,
                    'admin': u.is_superuser,
                    'managed': dept_ids(u, True),
                    'writable': [d for d in ids if allowed(u, d, 'upload')],
                    'capabilities': capabilities_data(u),
                },
                departments=list(deps.values()),
                items=list(Item.objects.filter(department_id__in=ids).values()),
                datasets=list(Dataset.objects.filter(item__department_id__in=ids).values()),
                photos=serialize_photos(photos, u),
                requests=serialize_requests(requests_qs, u),
                notices=list(Notice.objects.filter(user=u).order_by('-id').values()),
                radius=Config.objects.get_or_create(id=1)[0].radius,
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
