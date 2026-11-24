from django.contrib.auth.models import User
from django.http import JsonResponse
from .helpers import Problem, audit, form_fields, manager, writable
from .models import Config, Dataset, Department, FormPublication, Item, Membership
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
        radius = int(data['radius'])
        if radius < 100:
            raise Problem('半徑不得小於 100 公尺')
        Config.objects.update_or_create(id=1, defaults={'radius': radius})
        audit(u, path, 1, {'radius': radius})
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

    return None
