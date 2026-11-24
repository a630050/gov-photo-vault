import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import Audit, Anomaly, Membership, PersonPhotoLink, Request, Revision, Star, Photo
from .permissions import allowed, capabilities_data
from .face_engine import MODEL_KEY as FACE_MODEL_KEY
from .geocoding import reverse_geocode


class Problem(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


def audit(u, action, target, detail=None):
    Audit.objects.create(actor=u, action=action, target=str(target), detail=detail or {})


def dept_ids(u, manage=False):
    q = Membership.objects.filter(user=u)
    if manage:
        q = q.filter(role='supervisor')
    return list(q.values_list('department_id', flat=True))


def manager(u, department):
    return u.is_superuser or department in dept_ids(u, True)


def writable(u, department):
    return allowed(u, department, 'organize')


def visible(u):
    q = Photo.objects.select_related('dataset__item', 'uploader')
    if u.is_superuser:
        return q
    q = q.filter(dataset__item__department_id__in=dept_ids(u))
    return q.filter(
        Q(status='submitted')
        | Q(uploader=u, status='draft')
        | Q(dataset__item__department_id__in=dept_ids(u, True))
    )


def photo(u, id):
    p = visible(u).filter(id=id).first()
    if not p:
        raise Problem('找不到照片或沒有權限', 404)
    return p


def editable(u, p, capability='organize'):
    if not allowed(u, p.dataset.item.department_id, capability):
        raise Problem('沒有整理權限', 403)
    if p.dataset.archived:
        raise Problem('資料集已封存', 409)
    if p.status == 'recycled':
        raise Problem('照片已回收', 409)


def photo_snapshot(p):
    return dict(
        attrs=p.attrs,
        captured=p.captured.isoformat() if p.captured else None,
        lat=p.lat,
        lon=p.lon,
        time_confirmed=p.time_confirmed,
        dataset=p.dataset_id,
        status=p.status,
        version=p.version,
        fields=p.form_snapshot,
    )


def request_scope(u):
    q = Request.objects.all()
    if u.is_superuser:
        return q
    return q.filter(photo__dataset__item__department_id__in=dept_ids(u)).filter(
        Q(author=u) | Q(photo__dataset__item__department_id__in=dept_ids(u, True))
    )


def serialize_requests(requests, u):
    rows = list(requests)
    if not rows:
        return []
    managed_depts = set(dept_ids(u, True))
    photo_ids = {r.photo_id for r in rows}
    visible_ids = set(visible(u).filter(id__in=photo_ids).values_list('id', flat=True))
    result = []
    for r in rows:
        p = r.photo
        dept_id = p.dataset.item.department_id
        is_manager = u.is_superuser or dept_id in managed_depts
        result.append(dict(
            id=r.id,
            photo_id=p.id,
            photo_name=p.name,
            department=dept_id,
            dataset_name=p.dataset.name,
            author__username=r.author.username,
            kind=r.kind,
            reason=r.reason,
            payload=r.payload,
            before=r.before,
            state=r.state,
            created=r.created.isoformat(),
            closed_at=r.closed_at.isoformat() if r.closed_at else None,
            reviewer=r.reviewer.username if r.reviewer else None,
            decision_reason=r.decision_reason,
            can_review=r.state == 'pending' and is_manager and (r.author_id != u.id or u.is_superuser),
            can_withdraw=r.state == 'pending' and r.author_id == u.id,
            can_open=p.id in visible_ids,
            stale=p.version != r.base_version or p.status != 'submitted',
            events=[
                dict(
                    actor=e.actor.username if e.actor else '系統',
                    action=e.action,
                    detail=e.detail,
                    created=e.created.isoformat(),
                )
                for e in sorted(r.events.all(), key=lambda e: (e.created, e.id))
            ],
        ))
    return result


def serialize_request(r, u):
    return serialize_requests([r], u)[0]


def serialize_photos(photos, u):
    rows = list(photos)
    if not rows:
        return []

    active = bool(u.is_authenticated and u.is_active)
    caps_by_dept = capabilities_data(u) if active else {}
    managed_depts = set(dept_ids(u, True)) if active and not u.is_superuser else set()

    def has_cap(dept_id, key):
        if not active:
            return False
        if u.is_superuser and key not in ('people_view', 'face_search', 'people_manage'):
            return True
        return bool(caps_by_dept.get(str(dept_id), {}).get(key, False))

    photo_ids = [p.id for p in rows]
    starred_ids = set(Star.objects.filter(user=u, photo_id__in=photo_ids).values_list('photo_id', flat=True)) if active else set()

    anomalies_by_photo = {}
    for row in Anomaly.objects.filter(photo_id__in=photo_ids).values('photo_id', 'id', 'text', 'state', 'resolution'):
        pid = row.pop('photo_id')
        anomalies_by_photo.setdefault(pid, []).append(row)

    people_eligible = {
        p.id: p
        for p in rows
        if p.status == 'submitted' and has_cap(p.dataset.item.department_id, 'people_view')
    }
    people_by_photo = {}
    if people_eligible:
        links = PersonPhotoLink.objects.filter(
            active=True,
            person__active=True,
            face__photo_id__in=people_eligible.keys(),
            face__model_key=FACE_MODEL_KEY,
        ).select_related('person', 'face')
        for link in links:
            p = people_eligible.get(link.face.photo_id)
            if p and link.person.department_id == p.dataset.item.department_id and link.face.source_sha == p.sha:
                people_by_photo.setdefault(p.id, []).append({
                    'link': link.id,
                    'person': str(link.person_id),
                    'name': link.person.name,
                    'code': link.person.code,
                    'face': str(link.face_id),
                    'bbox': link.face.bbox,
                })

    result = []
    for p in rows:
        dept_id = p.dataset.item.department_id
        is_mgr = active and (u.is_superuser or dept_id in managed_depts)
        result.append(dict(
            people=people_by_photo.get(p.id, []),
            can_download=has_cap(dept_id, 'download'),
            extracted=p.extracted,
            id=p.id,
            name=p.name,
            dataset=p.dataset_id,
            item=p.dataset.item_id,
            department=dept_id,
            status=p.status,
            captured=p.captured.isoformat() if p.captured else '',
            time_confirmed=p.time_confirmed,
            lat=p.lat,
            lon=p.lon,
            attrs=p.attrs,
            fields=p.form_snapshot,
            form_version=p.form_version,
            tags=p.tags,
            version=p.version,
            burst_group=p.burst_group,
            burst_cover=p.burst_cover,
            county=p.admin_county,
            town=p.admin_town,
            village=p.admin_village,
            star=p.id in starred_ids,
            uploader=p.uploader.username,
            preview=f'/media/{p.id}/preview',
            original=f'/media/{p.id}/original',
            anomalies=anomalies_by_photo.get(p.id, []),
            can_write=has_cap(dept_id, 'organize'),
            can_manage=is_mgr,
        ))
    return result


def serialize(p, u):
    return serialize_photos([p], u)[0]


def update_photo_admin_region(p):
    county, town, village = reverse_geocode(p.lat, p.lon)
    p.admin_county = county
    p.admin_town = town
    p.admin_village = village


FIELD_TYPES = {'text', 'textarea', 'integer', 'number', 'date', 'select', 'multi', 'boolean'}


def dt(value):
    if not value:
        return None
    d = parse_datetime(value)
    if not d:
        raise Problem('拍攝時間格式錯誤')
    return timezone.make_aware(d, ZoneInfo('Asia/Taipei')) if timezone.is_naive(d) else d


def metadata(data):
    la, lo = data.get('lat'), data.get('lon')
    la = None if la in [None, ''] else float(la)
    lo = None if lo in [None, ''] else float(lo)
    if (la is None) != (lo is None):
        raise Problem('經緯度必須一起填寫')
    if la is not None and not (math.isfinite(la) and math.isfinite(lo) and -90 <= la <= 90 and -180 <= lo <= 180):
        raise Problem('經緯度超出合法範圍')
    return {
        'captured': dt(data.get('captured')),
        'lat': la,
        'lon': lo,
        'time_confirmed': bool(data.get('time_confirmed', False)),
        'attrs': data.get('attrs', {}),
    }


def form_fields(value):
    if not isinstance(value, list) or len(value) > 100:
        raise Problem('表單最多 100 個欄位')
    result = []
    keys = set()
    for f in value:
        if not isinstance(f, dict):
            raise Problem('欄位格式錯誤')
        key = f.get('key', '')
        label = str(f.get('label', '')).strip()
        kind = f.get('type', 'text')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', key) or key in keys:
            raise Problem('欄位識別無效或重複')
        if not label or len(label) > 100:
            raise Problem('請填寫 1 至 100 字的欄位名稱')
        if kind not in FIELD_TYPES:
            raise Problem('不支援的欄位類型')
        if not isinstance(f.get('required', False), bool):
            raise Problem('必填設定格式錯誤')
        field = {'key': key, 'label': label, 'type': kind, 'required': f.get('required', False)}
        if kind in ['select', 'multi']:
            options = f.get('options', [])
            if (
                not isinstance(options, list)
                or not options
                or len(options) > 100
                or any(not isinstance(x, str) or not x.strip() or len(x) > 100 for x in options)
            ):
                raise Problem(f'{label} 需設定 1 至 100 個有效選項')
            options = [x.strip() for x in options]
            if len(set(options)) != len(options):
                raise Problem(f'{label} 的選項不可重複')
            field['options'] = options
        keys.add(key)
        result.append(field)
    return result


def validate(p):
    if not isinstance(p.attrs, dict):
        raise Problem('屬性資料格式錯誤')
    for f in p.form_snapshot:
        v = p.attrs.get(f['key'])
        if f.get('required') and (v is None or v == '' or v == []):
            raise Problem(f"必填欄位：{f['label']}")
        if v in [None, '', []]:
            continue
        t = f.get('type', 'text')
        if t in ['integer', 'number']:
            try:
                n = float(v)
            except (ValueError, TypeError):
                raise Problem(f"{f['label']} 必須是數字")
            if not math.isfinite(n) or (t == 'integer' and not n.is_integer()):
                raise Problem('數值格式錯誤')
        if t in ['select', 'multi']:
            if t == 'multi' and not isinstance(v, list):
                raise Problem(f"{f['label']} 必須是多選資料")
            if t == 'select' and not isinstance(v, str):
                raise Problem(f"{f['label']} 必須是單選資料")
            values = v if isinstance(v, list) else [v]
            if any(x not in f.get('options', []) for x in values):
                raise Problem('選項不合法')
        if t == 'boolean' and not isinstance(v, bool):
            raise Problem(f"{f['label']} 必須是是／否")
        if t == 'date':
            try:
                datetime.strptime(v, '%Y-%m-%d')
            except (ValueError, TypeError):
                raise Problem(f"{f['label']} 日期格式錯誤")
        if t in ['text', 'textarea'] and not isinstance(v, str):
            raise Problem(f"{f['label']} 必須是文字")


def revision(p, u):
    Revision.objects.create(
        photo=p,
        version=p.version,
        actor=u,
        snapshot={
            'attrs': p.attrs,
            'captured': p.captured.isoformat() if p.captured else None,
            'lat': p.lat,
            'lon': p.lon,
            'dataset': p.dataset_id,
            'status': p.status,
            'fields': p.form_snapshot,
            'time_confirmed': p.time_confirmed,
        },
    )
