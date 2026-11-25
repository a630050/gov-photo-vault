import hashlib
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone
from .geocoding import clear_boundary_cache, load_boundary_index
from .helpers import Problem, audit, form_fields, manager, writable
from .models import AdminBoundaryVersion, Config, Dataset, Department, FormPublication, Item, Membership, Photo
from .permissions import CAPABILITIES, allowed


def handle_admin_api(path, data, u):
    if path == 'department':
        if not u.is_superuser:
            raise Problem('僅管理者可操作', 403)
        x = Department.objects.create(name=data['name'].strip())
        audit(u, path, x.id)
        return JsonResponse({'ok': True})

    if path == 'item':
        dep = int(data['department'])
        if not allowed(u, dep, 'forms'):
            raise Problem('沒有表單管理權限', 403)
        Department.objects.get(id=dep)
        fields = form_fields(data.get('fields', []))
        name = str(data.get('name', '')).strip()
        if not name or len(name) > 100:
            raise Problem('請填寫 1 至 100 字的表單名稱')
        if data.get('id'):
            x = Item.objects.select_for_update().get(id=data['id'], department_id=dep)
            if data.get('version') != x.version:
                raise Problem('表單已被更新，請重新載入再編輯', 409)
            old = {f['key']: f.get('type', 'text') for f in x.fields}
            if any(f['key'] in old and old[f['key']] != f['type'] for f in fields):
                raise Problem('改變欄位型別須建立新欄位識別')
            FormPublication.objects.get_or_create(
                template=x, version=x.version, defaults={'name': x.name, 'fields': x.fields}
            )
            x.fields = fields
            x.version += 1
            x.name = name
            x.active = data.get('active', True)
            x.save()
        else:
            x = Item.objects.create(department_id=dep, name=name, fields=fields)
        FormPublication.objects.create(template=x, version=x.version, name=x.name, fields=fields, actor=u)
        audit(u, 'publish_form', x.id, {'version': x.version, 'fields': fields})
        return JsonResponse({'ok': True, 'id': x.id, 'version': x.version})

    if path == 'dataset':
        item = Item.objects.get(id=data['item'])
        if not allowed(u, item.department_id, 'upload'):
            raise Problem('沒有上傳權限', 403)
        Department.objects.select_for_update().get(id=item.department_id)
        if not item.active:
            raise Problem('表單已停用', 409)
        name = str(data.get('name', '')).strip()
        if not name or len(name) > 120:
            raise Problem('請填寫 1 至 120 字的資料集名稱')
        if Dataset.objects.filter(item__department_id=item.department_id, name=name).exists():
            raise Problem('同科室已有同名資料集', 409)
        x = Dataset.objects.create(item=item, name=name, creator=u)
        audit(u, path, x.id)
        return JsonResponse({'ok': True})

    if path == 'assign_form':
        x = Dataset.objects.select_for_update().select_related('item').get(id=data['id'])
        if not allowed(u, x.item.department_id, 'forms'):
            raise Problem('沒有資料集表單管理權限', 403)
        if data.get('version') != x.version:
            raise Problem('資料集已變更，請重新載入', 409)
        if x.archived:
            raise Problem('請先重新開放資料集再調整表單', 409)
        target = Item.objects.get(id=data['item'])
        if target.department_id != x.item.department_id:
            raise Problem('只能指定同科室表單', 400)
        if not target.active:
            raise Problem('不能指定停用表單', 409)
        old = x.item_id
        x.item = target
        x.version += 1
        x.save()
        audit(u, path, x.id, {'before': old, 'after': target.id, 'existing_photos': 'unchanged'})
        return JsonResponse({'ok': True})

    if path == 'archive':
        x = Dataset.objects.select_for_update().select_related('item').get(id=data['id'])
        if not manager(u, x.item.department_id):
            raise Problem('需要主管權限', 403)
        x.archived = not x.archived
        x.version += 1
        x.save()
        audit(u, path, x.id, {'archived': x.archived})
        return JsonResponse({'ok': True})

    if path == 'rename':
        x = Dataset.objects.select_for_update().select_related('item').get(id=data['id'])
        if not (manager(u, x.item.department_id) or (x.creator == u and writable(u, x.item.department_id))):
            raise Problem('沒有改名權限', 403)
        if int(data['version']) != x.version:
            raise Problem('資料已變動', 409)
        old = x.name
        x.name = data['name'].strip()
        if not x.name or len(x.name) > 120:
            raise Problem('名稱需 1 至 120 字')
        if Dataset.objects.filter(item__department_id=x.item.department_id, name=x.name).exclude(id=x.id).exists():
            raise Problem('同科室已有同名資料集', 409)
        x.version += 1
        x.save()
        audit(u, path, x.id, {'before': old, 'after': x.name})
        return JsonResponse({'ok': True})

    if path == 'membership':
        if not u.is_superuser:
            raise Problem('需要管理者權限', 403)
        target = User.objects.select_for_update().get(id=data['user'])
        before = {
            'active': target.is_active,
            'memberships': list(Membership.objects.filter(user=target).values('department_id', 'role', 'permissions')),
        }
        if data.get('active') is not None:
            if not isinstance(data['active'], bool):
                raise Problem('帳號啟用設定必須為是或否')
            if (
                target.is_superuser
                and not data['active']
                and User.objects.filter(is_superuser=True, is_active=True).count() <= 1
            ):
                raise Problem('不可停用最後管理者', 409)
            target.is_active = bool(data['active'])
            target.save()
        else:
            Department.objects.get(id=data['department'])
            if data.get('remove'):
                Membership.objects.filter(user=target, department_id=data['department']).delete()
            else:
                role = data['role']
                permissions = data.get('permissions', {})
                if role not in ['member', 'supervisor', 'viewer']:
                    raise Problem('角色錯誤')
                if not isinstance(permissions, dict) or any(
                    k not in CAPABILITIES or not isinstance(v, bool) for k, v in permissions.items()
                ):
                    raise Problem('權限設定格式錯誤')
                Membership.objects.update_or_create(
                    user=target,
                    department_id=data['department'],
                    defaults={'role': role, 'permissions': permissions},
                )
        audit(
            u,
            path,
            target.id,
            {
                'before': before,
                'after': {
                    'active': target.is_active,
                    'memberships': list(
                        Membership.objects.filter(user=target).values('department_id', 'role', 'permissions')
                    ),
                },
            },
        )
        return JsonResponse({'ok': True})

    if path == 'config':
        if not u.is_superuser:
            raise Problem('需要管理者權限', 403)
        cfg, _ = Config.objects.get_or_create(id=1)
        if 'radius' in data:
            radius = int(data['radius'])
            if radius < 100:
                raise Problem('半徑不得小於 100 公尺')
            cfg.radius = radius
        if 'burst_gap_seconds' in data:
            gap = int(data['burst_gap_seconds'])
            if not 1 <= gap <= 300:
                raise Problem('連拍相鄰時間差須介於 1 至 300 秒')
            cfg.burst_gap_seconds = gap
        if 'burst_dhash_max' in data:
            dmax = int(data['burst_dhash_max'])
            if not 0 <= dmax <= 32:
                raise Problem('連拍指紋距離門檻須介於 0 至 32')
            cfg.burst_dhash_max = dmax
        if 'burst_span_seconds' in data:
            span = int(data['burst_span_seconds'])
            if not 1 <= span <= 3600:
                raise Problem('連拍整組最大跨度須介於 1 至 3600 秒')
            cfg.burst_span_seconds = span
        if cfg.burst_span_seconds < cfg.burst_gap_seconds:
            raise Problem('整組最大時間跨度不得小於相鄰時間差')
        cfg.save()
        audit(
            u,
            path,
            1,
            {
                'radius': cfg.radius,
                'burst_gap_seconds': cfg.burst_gap_seconds,
                'burst_dhash_max': cfg.burst_dhash_max,
                'burst_span_seconds': cfg.burst_span_seconds,
            },
        )
        return JsonResponse({'ok': True})

    if path == 'user':
        if not u.is_superuser:
            raise Problem('需要管理者權限', 403)
        if data.get('role', 'member') not in ['member', 'supervisor', 'viewer']:
            raise Problem('角色錯誤')
        x = User.objects.create_user(data['username'], password=data['password'])
        Membership.objects.create(user=x, department_id=data['department'], role=data.get('role', 'member'))
        audit(u, path, x.id)
        return JsonResponse({'ok': True})

    if path == 'boundary/list':
        if not u.is_superuser:
            raise Problem('僅系統管理者可查看行政區邊界版本', 403)
        versions = [
            {
                'id': b.id,
                'version': b.version,
                'source': b.source,
                'published_date': b.published_date,
                'sha256': b.sha256,
                'crs': b.crs,
                'file_path': b.file_path,
                'active': b.active,
                'activated_at': b.activated_at.isoformat() if b.activated_at else None,
                'imported_by': b.imported_by.username if b.imported_by else '系統預設',
                'created': b.created.isoformat(),
            }
            for b in AdminBoundaryVersion.objects.select_related('imported_by').order_by('-active', '-activated_at', '-id')
        ]
        status_counts = {
            (r['admin_status'] or 'unknown'): r['count']
            for r in Photo.objects.values('admin_status').annotate(count=Count('id'))
        }
        return JsonResponse({'versions': versions, 'status_counts': status_counts})

    if path == 'boundary/save':
        if not u.is_superuser:
            raise Problem('僅系統管理者可註冊行政區邊界版本', 403)
        version = str(data.get('version') or '').strip()
        source = str(data.get('source') or '').strip()
        published_date = str(data.get('published_date') or '').strip()
        crs = str(data.get('crs') or 'EPSG:4326 (WGS84)').strip()
        file_path = str(data.get('file_path') or '').strip()
        expected_sha = str(data.get('sha256') or '').strip().lower()
        if not version or len(version) > 60:
            raise Problem('請輸入 1 至 60 字的邊界版本識別碼', 422)
        if not source or len(source) > 250:
            raise Problem('請輸入來源與授權說明', 422)
        if not file_path or '..' in Path(file_path).parts:
            raise Problem('邊界檔案路徑無效', 422)
        base_dir = Path(settings.BASE_DIR).resolve()
        candidate = (base_dir / file_path).resolve()
        try:
            candidate.relative_to(base_dir)
        except ValueError as exc:
            raise Problem('邊界檔案必須位於專案允許目錄內', 422) from exc
        if not candidate.is_file():
            raise Problem('找不到指定的邊界檔案', 404)
        actual_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if expected_sha and expected_sha != actual_sha:
            raise Problem('邊界檔案校驗碼不相符', 422)
        try:
            clear_boundary_cache()
            load_boundary_index(str(candidate), expected_sha256=actual_sha)
        except Exception as exc:
            raise Problem(f'邊界檔案驗證失敗：{exc}', 422) from exc
        activate = bool(data.get('activate', True))
        if activate:
            AdminBoundaryVersion.objects.update(active=False)
        obj, created = AdminBoundaryVersion.objects.update_or_create(
            version=version,
            defaults={
                'source': source,
                'published_date': published_date,
                'sha256': actual_sha,
                'crs': crs,
                'file_path': file_path,
                'active': activate,
                'activated_at': timezone.now(),
                'imported_by': u,
            },
        )
        audit(u, 'boundary_save', obj.id, {'version': version, 'sha256': actual_sha, 'active': activate, 'created': created})
        return JsonResponse({'ok': True, 'version': obj.version, 'sha256': actual_sha})

    if path == 'boundary/activate':
        if not u.is_superuser:
            raise Problem('僅系統管理者可切換行政區邊界版本', 403)
        version = str(data.get('version') or '').strip()
        obj = AdminBoundaryVersion.objects.filter(version=version).first()
        if not obj:
            raise Problem('找不到指定的邊界版本', 404)
        AdminBoundaryVersion.objects.update(active=False)
        obj.active = True
        obj.activated_at = timezone.now()
        obj.save(update_fields=['active', 'activated_at'])
        clear_boundary_cache()
        audit(u, 'boundary_activate', obj.id, {'version': obj.version})
        return JsonResponse({'ok': True, 'version': obj.version})

    return None
