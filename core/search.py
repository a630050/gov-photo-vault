"""One authorized result set shared by map clusters, photo search pagination, and selection tokens.

SQLite development backend: fast SQL count/aggregations when JSON/tag predicates are absent,
and lightweight column projection when JSON/tag predicates are active.
"""
from datetime import date, datetime, time, timedelta
import math
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from django.core import signing
from django.db.models import Count, F, Q
from .models import Anomaly, Dataset, Membership, Star

DEFAULT_LIMIT = 100
MAX_LIMIT = 200
SELECTION_SALT = 'photo-search-selection-v1'
SELECTION_MAX_AGE = 7200


class SearchError(ValueError):
    pass


def empty(value):
    return value is None or value == '' or value == []


def custom_match(photo, rule):
    key, kind, op = rule['key'], rule['type'], rule['op']
    schema = next((f for f in photo.form_snapshot if f['key'] == key and f.get('type', 'text') == kind), None)
    value = photo.attrs.get(key) if schema else None
    if op == 'missing':
        return empty(value)
    if empty(value):
        return False
    if op == 'contains':
        return str(rule.get('value', '')).casefold() in str(value).casefold()
    if op == 'in':
        values = value if isinstance(value, list) else [value]
        return any(x in rule['values'] for x in values)
    if op == 'equals':
        return isinstance(value, bool) and value == rule['value']
    if op == 'range':
        try:
            convert = date.fromisoformat if kind == 'date' else float
            actual = convert(value)
            if kind != 'date' and not math.isfinite(actual):
                return False
            return (empty(rule.get('min')) or actual >= convert(rule['min'])) and (
                empty(rule.get('max')) or actual <= convert(rule['max'])
            )
        except (ValueError, TypeError, OverflowError):
            return False
    return False


def mercator_bin(lat, lon, zoom, grid_px=55):
    z = max(0, min(22, int(zoom)))
    px_size = max(16, min(256, int(grid_px)))
    scale = 256.0 * (2**z)
    clamped_lat = max(-85.05112878, min(85.05112878, float(lat)))
    lat_rad = math.radians(clamped_lat)
    x = (float(lon) + 180.0) / 360.0 * scale
    y = (0.5 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / (2.0 * math.pi)) * scale
    return math.floor(x / px_size), math.floor(y / px_size)


def _parse_bounds(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SearchError('地圖範圍格式錯誤')
    try:
        north = float(raw['north'])
        south = float(raw['south'])
        east = float(raw['east'])
        west = float(raw['west'])
    except (KeyError, TypeError, ValueError) as exc:
        raise SearchError('地圖邊界座標不完整') from exc
    if not all(math.isfinite(v) for v in (north, south, east, west)):
        raise SearchError('地圖邊界座標不合法')
    if south > north or north > 90 or south < -90 or east > 180 or west < -180:
        raise SearchError('地圖邊界範圍超出允許值')
    return {'north': north, 'south': south, 'east': east, 'west': west}


def _apply_bounds(q, bounds):
    if not bounds:
        return q
    q = q.filter(lat__isnull=False, lon__isnull=False, lat__gte=bounds['south'], lat__lte=bounds['north'])
    if bounds['west'] <= bounds['east']:
        return q.filter(lon__gte=bounds['west'], lon__lte=bounds['east'])
    return q.filter(Q(lon__gte=bounds['west']) | Q(lon__lte=bounds['east']))


def _parse_cluster(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SearchError('群聚條件格式錯誤')
    try:
        zoom = int(raw['zoom'])
        bx = int(raw['bx'])
        by = int(raw['by'])
        grid_px = int(raw.get('grid_px', 55))
    except (KeyError, TypeError, ValueError) as exc:
        raise SearchError('群聚識別參數無效') from exc
    if not 0 <= zoom <= 22 or not 16 <= grid_px <= 256:
        raise SearchError('群聚縮放或網格大小無效')
    return {'zoom': zoom, 'bx': bx, 'by': by, 'grid_px': grid_px}


def _prepare_search(queryset, user, criteria):
    if not isinstance(criteria, dict):
        raise SearchError('搜尋條件格式錯誤')
    status = criteria.get('status', 'submitted')
    if status not in ('submitted', 'draft', 'recycled'):
        raise SearchError('未知照片狀態')
    q = queryset.filter(status=status)

    raw_deps = criteria.get('departments', [])
    raw_ds = criteria.get('datasets', [])
    if not isinstance(raw_deps, list) or not isinstance(raw_ds, list):
        raise SearchError('科室或資料集篩選格式錯誤')
    departments = [int(x) for x in raw_deps]
    datasets = [int(x) for x in raw_ds]
    if departments or datasets:
        q = q.filter(Q(dataset__item__department_id__in=departments) | Q(dataset_id__in=datasets))

    raw_ids = criteria.get('ids')
    clean_ids = None
    if raw_ids is not None:
        if not isinstance(raw_ids, list) or len(raw_ids) > 500:
            raise SearchError('指定照片清單格式錯誤或超過 500 筆')
        clean_ids = [int(x) for x in raw_ids]
        q = q.filter(id__in=clean_ids)

    burst_group = str(criteria.get('burst_group') or '').strip()
    if burst_group:
        if len(burst_group) > 36:
            raise SearchError('連拍群組代碼無效')
        q = q.filter(burst_group=burst_group)

    text = str(criteria.get('text') or '')[:200]
    if text:
        q = q.filter(name__icontains=text)

    region_vals = {}
    for param, col in (('county', 'admin_county'), ('town', 'admin_town'), ('village', 'admin_village')):
        val = str(criteria.get(param, '') or '').strip()
        region_vals[param] = val
        if val == '__missing__':
            q = q.filter(**{col: ''})
        elif val:
            q = q.filter(**{col: val})

    star = bool(criteria.get('star'))
    if star:
        q = q.filter(id__in=Star.objects.filter(user=user).values('photo_id'))

    anomaly = criteria.get('anomaly', '')
    if anomaly:
        if anomaly not in ('any', 'none', 'pending', 'confirmed', 'dismissed'):
            raise SearchError('未知異常狀態')
        cases = Anomaly.objects.all()
        if anomaly not in ('any', 'none'):
            cases = cases.filter(state=anomaly)
        q = q.exclude(id__in=cases.values('photo_id')) if anomaly == 'none' else q.filter(id__in=cases.values('photo_id'))

    missing_time = q.filter(captured__isnull=True)
    start, end = criteria.get('from'), criteria.get('to')
    try:
        if start and end and date.fromisoformat(start) > date.fromisoformat(end):
            raise SearchError('拍攝起日不可晚於迄日')
        tz = ZoneInfo('Asia/Taipei')
        if start:
            q = q.filter(captured__gte=datetime.combine(date.fromisoformat(start), time.min, tzinfo=tz))
        if end:
            q = q.filter(captured__lt=datetime.combine(date.fromisoformat(end) + timedelta(days=1), time.min, tzinfo=tz))
    except (ValueError, OverflowError) as exc:
        raise SearchError('拍攝日期範圍不合法') from exc

    rules = criteria.get('fields', [])
    if not isinstance(rules, list) or len(rules) > 100:
        raise SearchError('自訂欄位條件過多')
    if rules:
        if len(datasets) != 1 or departments:
            raise SearchError('請只選一個資料集再篩選自訂欄位')
        authorized = queryset.filter(dataset_id=datasets[0], status=status)
        definitions = {
            (f['key'], f.get('type', 'text'))
            for snapshot in authorized.values_list('form_snapshot', flat=True)
            for f in snapshot
        }
        ds = Dataset.objects.select_related('item').filter(id=datasets[0]).first()
        if ds and (user.is_superuser or Membership.objects.filter(user=user, department_id=ds.item.department_id).exists()):
            definitions.update((f['key'], f.get('type', 'text')) for f in ds.item.fields)
        for rule in rules:
            if not isinstance(rule, dict) or (rule.get('key'), rule.get('type')) not in definitions:
                raise SearchError('欄位不存在或不屬於此資料集')
            kind, op = rule['type'], rule.get('op')
            allowed = {
                'text': ('contains',),
                'textarea': ('contains',),
                'integer': ('range',),
                'number': ('range',),
                'date': ('range',),
                'select': ('in',),
                'multi': ('in',),
                'boolean': ('equals',),
            }
            if op != 'missing' and op not in allowed.get(kind, ()):
                raise SearchError('欄位搜尋方式不合法')
            if op == 'equals' and not isinstance(rule.get('value'), bool):
                raise SearchError('是／否條件不合法')
            if op == 'in' and (not isinstance(rule.get('values'), list) or not rule['values']):
                raise SearchError('請選擇欄位選項')
            if op == 'range':
                try:
                    convert = date.fromisoformat if kind == 'date' else float
                    bounds_vals = [convert(rule[key]) for key in ('min', 'max') if not empty(rule.get(key))]
                    if kind != 'date' and any(not math.isfinite(v) for v in bounds_vals):
                        raise ValueError()
                    if len(bounds_vals) == 2 and bounds_vals[0] > bounds_vals[1]:
                        raise ValueError()
                except (ValueError, TypeError, OverflowError) as exc:
                    raise SearchError('欄位範圍不合法') from exc

    tags = criteria.get('tags', [])
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise SearchError('標籤條件格式錯誤')

    bounds = _parse_bounds(criteria.get('bounds'))
    cluster = _parse_cluster(criteria.get('cluster'))
    if bounds:
        q = _apply_bounds(q, bounds)
        missing_time = _apply_bounds(missing_time, bounds)
    if cluster:
        q = q.filter(lat__isnull=False, lon__isnull=False)
        missing_time = missing_time.filter(lat__isnull=False, lon__isnull=False)

    has_time_filter = bool(start or end)
    normalized = {
        'status': status,
        'departments': departments,
        'datasets': datasets,
        'text': text,
        'county': region_vals['county'],
        'town': region_vals['town'],
        'village': region_vals['village'],
        'star': star,
        'anomaly': anomaly,
        'from': start or '',
        'to': end or '',
        'fields': rules,
        'tags': tags,
        'bounds': bounds,
        'cluster': cluster,
        'burst_group': burst_group,
    }
    if clean_ids is not None:
        normalized['ids'] = clean_ids
    return q, normalized, tags, rules, (missing_time if has_time_filter else None), cluster


def _count_matching_rows(ordered_q, tags, rules, cluster_spec=None):
    if not tags and not rules and not cluster_spec:
        return ordered_q.count()
    cols = ['id', 'tags', 'lat', 'lon']
    if rules:
        cols.extend(['attrs', 'form_snapshot'])
    cnt = 0
    for row in ordered_q.values(*cols).iterator(chunk_size=1000):
        if tags and not any(t in row['tags'] for t in tags):
            continue
        if rules:
            proxy = SimpleNamespace(attrs=row['attrs'], form_snapshot=row['form_snapshot'])
            if not all(custom_match(proxy, rule) for rule in rules):
                continue
        if cluster_spec:
            if row['lat'] is None or row['lon'] is None:
                continue
            bx, by = mercator_bin(row['lat'], row['lon'], cluster_spec['zoom'], cluster_spec['grid_px'])
            if (bx, by) != (cluster_spec['bx'], cluster_spec['by']):
                continue
        cnt += 1
    return cnt


def _matching_ids_and_stats(q, tags, rules, cluster_spec=None):
    ordered_q = q.order_by(F('captured').desc(nulls_last=True), '-id')
    if not tags and not rules and not cluster_spec:
        total = ordered_q.count()
        located = ordered_q.filter(lat__isnull=False, lon__isnull=False).count()
        region_stats = [
            {'county': r['admin_county'], 'town': r['admin_town'], 'count': r['count']}
            for r in ordered_q.exclude(admin_county='')
            .values('admin_county', 'admin_town')
            .annotate(count=Count('id'))
            .order_by('-count', 'admin_county', 'admin_town')
        ]
        burst_matched_counts = {
            r['burst_group']: r['count']
            for r in ordered_q.filter(status='submitted')
            .exclude(burst_group='')
            .values('burst_group')
            .annotate(count=Count('id'))
        }
        return None, ordered_q, total, located, total - located, region_stats, burst_matched_counts

    need_rules = bool(rules)
    cols = ['id', 'tags', 'lat', 'lon', 'admin_county', 'admin_town', 'burst_group', 'status']
    if need_rules:
        cols.extend(['attrs', 'form_snapshot'])
    matched_ids = []
    located = 0
    region_counts = {}
    burst_counts = {}
    for row in ordered_q.values(*cols).iterator(chunk_size=1000):
        if tags and not any(t in row['tags'] for t in tags):
            continue
        if need_rules:
            proxy = SimpleNamespace(attrs=row['attrs'], form_snapshot=row['form_snapshot'])
            if not all(custom_match(proxy, rule) for rule in rules):
                continue
        if cluster_spec:
            if row['lat'] is None or row['lon'] is None:
                continue
            bx, by = mercator_bin(row['lat'], row['lon'], cluster_spec['zoom'], cluster_spec['grid_px'])
            if (bx, by) != (cluster_spec['bx'], cluster_spec['by']):
                continue
        matched_ids.append(row['id'])
        if row['lat'] is not None and row['lon'] is not None:
            located += 1
        if row['admin_county']:
            key = (row['admin_county'], row['admin_town'])
            region_counts[key] = region_counts.get(key, 0) + 1
        if row['burst_group'] and row['status'] == 'submitted':
            burst_counts[row['burst_group']] = burst_counts.get(row['burst_group'], 0) + 1

    total = len(matched_ids)
    region_stats = [
        {'county': k[0], 'town': k[1], 'count': v}
        for k, v in sorted(region_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return matched_ids, None, total, located, total - located, region_stats, burst_counts


def search_photos(queryset, user, criteria):
    q, normalized, tags, rules, missing_time_q, cluster_spec = _prepare_search(queryset, user, criteria)
    try:
        limit = int(criteria.get('limit', DEFAULT_LIMIT))
        offset = int(criteria.get('offset', 0))
    except (TypeError, ValueError) as exc:
        raise SearchError('分頁參數格式錯誤') from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise SearchError(f'每頁筆數須為 1 至 {MAX_LIMIT}')
    if offset < 0:
        raise SearchError('分頁位移不可為負數')

    excluded_missing_time = _count_matching_rows(missing_time_q, tags, rules, cluster_spec) if missing_time_q is not None else 0

    matched_ids, ordered_q, total, located, missing_location, region_stats, burst_counts = _matching_ids_and_stats(
        q, tags, rules, cluster_spec
    )
    if ordered_q is not None:
        page_rows = list(ordered_q[offset : offset + limit])
        page_ids = [p.id for p in page_rows]
    else:
        page_ids = matched_ids[offset : offset + limit]
        row_map = {p.id: p for p in queryset.filter(id__in=page_ids)}
        page_rows = [row_map[pid] for pid in page_ids if pid in row_map]

    has_next = (offset + len(page_rows)) < total
    next_offset = (offset + len(page_rows)) if has_next else None
    token = signing.dumps({'user_id': user.id, 'criteria': normalized}, salt=SELECTION_SALT)
    return {
        'rows': page_rows,
        'ids': page_ids,
        'offset': offset,
        'limit': limit,
        'total': total,
        'located': located,
        'missing_location': missing_location,
        'excluded_missing_time': excluded_missing_time,
        'region_stats': region_stats,
        'burst_matched_counts': burst_counts,
        'has_next': has_next,
        'next_offset': next_offset,
        'selection_token': token,
    }


def cluster_map_photos(queryset, user, criteria):
    clean_criteria = {**(criteria if isinstance(criteria, dict) else {}), 'cluster': None}
    q, _, tags, rules, _, _ = _prepare_search(queryset, user, clean_criteria)
    try:
        zoom = int(criteria.get('zoom', 11))
        grid_px = int(criteria.get('grid_px', 55))
    except (TypeError, ValueError) as exc:
        raise SearchError('地圖縮放層級格式錯誤') from exc
    if not 0 <= zoom <= 22 or not 16 <= grid_px <= 256:
        raise SearchError('地圖縮放或網格大小無效')

    q = q.filter(lat__isnull=False, lon__isnull=False).order_by(F('captured').desc(nulls_last=True), '-id')
    cols = ['id', 'name', 'lat', 'lon', 'tags']
    if rules:
        cols.extend(['attrs', 'form_snapshot'])

    bins = {}
    total_located = 0
    for row in q.values(*cols).iterator(chunk_size=2000):
        if tags and not any(t in row['tags'] for t in tags):
            continue
        if rules:
            proxy = SimpleNamespace(attrs=row['attrs'], form_snapshot=row['form_snapshot'])
            if not all(custom_match(proxy, rule) for rule in rules):
                continue
        total_located += 1
        bx, by = mercator_bin(row['lat'], row['lon'], zoom, grid_px)
        bucket = bins.get((bx, by))
        if bucket is None:
            bins[(bx, by)] = {
                'bx': bx,
                'by': by,
                'zoom': zoom,
                'grid_px': grid_px,
                'lat_sum': row['lat'],
                'lon_sum': row['lon'],
                'count': 1,
                'first_id': row['id'],
                'first_name': row['name'],
            }
        else:
            bucket['lat_sum'] += row['lat']
            bucket['lon_sum'] += row['lon']
            bucket['count'] += 1

    clusters = [
        {
            'bx': b['bx'],
            'by': b['by'],
            'zoom': b['zoom'],
            'grid_px': b['grid_px'],
            'lat': round(b['lat_sum'] / b['count'], 6),
            'lon': round(b['lon_sum'] / b['count'], 6),
            'count': b['count'],
            'single_photo_id': b['first_id'] if b['count'] == 1 else None,
            'single_photo_name': b['first_name'] if b['count'] == 1 else '',
        }
        for b in bins.values()
    ]
    return {'clusters': clusters, 'total_located': total_located, 'zoom': zoom, 'grid_px': grid_px}


def resolve_selection_ids(queryset, user, selection_token, excluded_photos=None):
    """Resolve a signed cross-page selection token against live permissions and DB state."""
    from .helpers import Problem

    try:
        payload = signing.loads(selection_token, salt=SELECTION_SALT, max_age=SELECTION_MAX_AGE)
    except signing.SignatureExpired as exc:
        raise Problem('搜尋選取憑證已過期，請重新搜尋後再試', 409) from exc
    except signing.BadSignature as exc:
        raise Problem('搜尋選取憑證無效，請重新搜尋', 403) from exc

    if payload.get('user_id') != user.id:
        raise Problem('選取憑證不屬於目前登入者', 403)

    excluded = set()
    if excluded_photos is not None:
        if not isinstance(excluded_photos, list) or any(type(x) is not int for x in excluded_photos):
            raise Problem('排除清單格式錯誤', 422)
        excluded = set(excluded_photos)

    try:
        q, _, tags, rules, _, cluster_spec = _prepare_search(queryset, user, payload['criteria'])
    except SearchError as exc:
        raise Problem(str(exc), 422) from exc

    matched_ids, ordered_q, _, _, _, _, _ = _matching_ids_and_stats(q, tags, rules, cluster_spec)
    if ordered_q is not None:
        all_ids = [pid for pid in ordered_q.values_list('id', flat=True) if pid not in excluded]
    else:
        all_ids = [pid for pid in matched_ids if pid not in excluded]
    return all_ids


def authorized_filter_options(queryset, user, criteria=None):
    """Return authorized tags, admin regions, and custom field definitions without loading all photos."""
    criteria = criteria or {}
    status = criteria.get('status', 'submitted')
    if status not in ('submitted', 'draft', 'recycled'):
        raise SearchError('未知照片狀態')
    q = queryset.filter(status=status)

    raw_deps = criteria.get('departments', [])
    raw_ds = criteria.get('datasets', [])
    departments = [int(x) for x in raw_deps] if isinstance(raw_deps, list) else []
    datasets = [int(x) for x in raw_ds] if isinstance(raw_ds, list) else []
    if departments or datasets:
        q = q.filter(Q(dataset__item__department_id__in=departments) | Q(dataset_id__in=datasets))

    tag_set = set()
    for tag_list in q.exclude(tags=[]).values_list('tags', flat=True).iterator(chunk_size=1000):
        for t in tag_list:
            if t:
                tag_set.add(t)

    counties = sorted(
        c for c in q.exclude(admin_county='').values_list('admin_county', flat=True).distinct() if c
    )
    town_q = q
    county = str(criteria.get('county') or '').strip()
    if county and county != '__missing__':
        town_q = town_q.filter(admin_county=county)
    towns = sorted(t for t in town_q.exclude(admin_town='').values_list('admin_town', flat=True).distinct() if t)

    village_q = town_q
    town = str(criteria.get('town') or '').strip()
    if town:
        village_q = village_q.filter(admin_town=town)
    villages = sorted(
        v for v in village_q.exclude(admin_village='').values_list('admin_village', flat=True).distinct() if v
    )

    field_defs = []
    if len(datasets) == 1 and not departments:
        ds = Dataset.objects.select_related('item').filter(id=datasets[0]).first()
        if ds and (user.is_superuser or Membership.objects.filter(user=user, department_id=ds.item.department_id).exists()):
            seen = {}
            def add_field(f, historical):
                ftype = f.get('type', 'text')
                key_id = f"{f['key']}|{ftype}"
                if key_id not in seen:
                    entry = {**f, 'type': ftype, 'historical': historical}
                    seen[key_id] = entry
                    field_defs.append(entry)
                elif f.get('options'):
                    existing_opts = list(seen[key_id].get('options') or [])
                    for opt in f['options']:
                        if opt not in existing_opts:
                            existing_opts.append(opt)
                    seen[key_id]['options'] = existing_opts

            for f in ds.item.fields:
                add_field(f, False)
            seen_snapshots = set()
            for snapshot in q.filter(dataset_id=ds.id).values_list('form_snapshot', flat=True).iterator(chunk_size=500):
                sig = str(snapshot)
                if sig in seen_snapshots:
                    continue
                seen_snapshots.add(sig)
                for f in snapshot:
                    add_field(f, True)

    return {
        'tags': sorted(tag_set),
        'counties': counties,
        'towns': towns,
        'villages': villages,
        'fields': field_defs,
    }
