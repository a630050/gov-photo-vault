"""Shared Integration Service Layer used identically by Integration REST API, MCP, and Picker."""
from datetime import date, datetime, time as dt_time, timedelta
import hashlib
import io
import math
import uuid
from zoneinfo import ZoneInfo
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from PIL import Image, ImageOps, UnidentifiedImageError
from .integration_auth import (
    IntegrationContext,
    IntegrationError,
    can_download_original,
    can_preview,
    can_report_import,
    effective_dataset_ids,
    effective_photo_queryset,
    integration_audit,
    issue_image_ticket,
    normalize_allowed_fields,
    normalize_capabilities,
    require_capability,
    verify_image_ticket,
)
from .models import (
    Dataset,
    Photo,
    PhotoEmbedding,
    PhotoOcr,
    ReportImportItem,
    ReportImportJob,
)
from .ocr import _snippet as ocr_snippet, ocr_coverage, ocr_search
from .ocr_engine import (
    MODEL_KEY as OCR_MODEL_KEY,
    ModelUnavailable as OcrUnavailable,
    normalize_query as ocr_normalize_query,
    readiness as ocr_readiness,
)
from .search import SearchError, _apply_bounds, _parse_bounds, custom_match
from .semantic import valid_vector
from .semantic_engine import (
    MODEL_KEY as SEMANTIC_MODEL_KEY,
    ModelUnavailable as SemanticUnavailable,
    readiness as semantic_readiness,
    text_vector,
)

TAIPEI_TZ = ZoneInfo('Asia/Taipei')
REPORT_MAX_EDGE = 2048
MCP_PREVIEW_MAX_EDGE = 640
MCP_PREVIEW_MAX_BYTES = 220 * 1024


def _haversine_meters(lat1, lon1, lat2, lon2):
    r_lat1, r_lat2 = math.radians(lat1), math.radians(lat2)
    dlat = r_lat2 - r_lat1
    dlon = math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(r_lat1) * math.cos(r_lat2) * math.sin(dlon / 2) ** 2
    return 6371008.8 * 2 * math.asin(math.sqrt(max(0.0, min(1.0, h))))


def _allowed_custom_field_keys(allowed_fields: dict, dataset_id: int, all_keys=None) -> set:
    cf = allowed_fields.get('custom_fields') or {}
    raw = cf.get(str(dataset_id))
    if not raw:
        return set()
    if '*' in raw and all_keys is not None:
        return set(all_keys)
    return set(raw)


def _format_captured_taipei(dt_val):
    if dt_val is None:
        return None
    if timezone.is_naive(dt_val):
        dt_val = dt_val.replace(tzinfo=TAIPEI_TZ)
    return dt_val.astimezone(TAIPEI_TZ).isoformat()


def _parse_time_bound(raw, is_end=False):
    """Parse YYYY-MM-DD or timezone-aware ISO datetime; default business timezone is Asia/Taipei."""
    if raw is None or str(raw).strip() == '':
        return None, None
    s = str(raw).strip()
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        try:
            d = date.fromisoformat(s)
        except ValueError as exc:
            raise IntegrationError('INVALID_CRITERIA', f'日期格式不合法：{s}', status=422) from exc
        if is_end:
            dt_bound = datetime.combine(d + timedelta(days=1), dt_time.min, tzinfo=TAIPEI_TZ)
            return dt_bound, ('date_inclusive', d.isoformat())
        dt_bound = datetime.combine(d, dt_time.min, tzinfo=TAIPEI_TZ)
        return dt_bound, ('date_inclusive', d.isoformat())
    parsed = parse_datetime(s)
    if not parsed or timezone.is_naive(parsed):
        raise IntegrationError(
            'INVALID_CRITERIA',
            '日期時間須提供明確時區（例如 +08:00），或使用 YYYY-MM-DD 日期格式',
            status=422,
        )
    return parsed.astimezone(TAIPEI_TZ), ('datetime', parsed.astimezone(TAIPEI_TZ).isoformat())


def serialize_integration_photo(
    photo: Photo,
    ctx: IntegrationContext,
    allowed_fields: dict = None,
    match_reason: dict = None,
    include_preview_ticket: bool = False,
) -> dict:
    af = allowed_fields if allowed_fields is not None else normalize_allowed_fields(ctx.system.allowed_fields)
    all_keys = [f.get('key') for f in (photo.form_snapshot or []) if isinstance(f, dict) and f.get('key')]
    permitted_keys = _allowed_custom_field_keys(af, photo.dataset_id, all_keys=all_keys)
    filtered_attrs = {k: v for k, v in (photo.attrs or {}).items() if k in permitted_keys}

    warnings = []
    if photo.captured is None:
        warnings.append('missing_captured_time')
    elif not photo.time_confirmed:
        warnings.append('unconfirmed_captured_time')
    if photo.lat is None or photo.lon is None:
        warnings.append('missing_location')
    if photo.dataset.archived:
        warnings.append('dataset_archived')

    allow_preview = can_preview(ctx, photo)
    thumb_info = {
        'method': 'preview_ticket',
        'ticket_endpoint': f'/integration/v1/photos/{photo.id}/preview-ticket',
    }
    if include_preview_ticket and allow_preview:
        thumb_info['ticket'] = issue_image_ticket(ctx, photo.id, variant='preview', purpose='preview')

    item = {
        'photo_id': photo.id,
        'dataset_id': photo.dataset_id,
        'dataset_name': photo.dataset.name,
        'name': photo.name,
        'version': photo.version,
        'source_sha': photo.sha,
        'captured': _format_captured_taipei(photo.captured) if af.get('captured', True) else None,
        'timezone': 'Asia/Taipei',
        'time_confirmed': bool(photo.time_confirmed) if af.get('captured', True) else None,
        'location': (
            {
                'lat': photo.lat,
                'lon': photo.lon,
                'county': photo.admin_county,
                'town': photo.admin_town,
                'village': photo.admin_village,
            }
            if af.get('location', True)
            else None
        ),
        'tags': list(photo.tags or []) if af.get('tags', True) else None,
        'attrs': filtered_attrs,
        'thumbnail': thumb_info,
        'permissions': {
            'can_preview': allow_preview,
            'can_report_import': can_report_import(ctx, photo),
            'can_original_download': can_download_original(ctx, photo),
        },
        'match_reason': match_reason or {'mode': 'general'},
        'quality_warnings': warnings,
    }
    return item


def service_capabilities(ctx: IntegrationContext) -> dict:
    caps = normalize_capabilities(ctx.system.capabilities)
    af = normalize_allowed_fields(ctx.system.allowed_fields)
    ds_ids = effective_dataset_ids(ctx)
    effective_caps = dict(caps)
    if ctx.scopes is not None:
        scope_map = {
            'search': 'photos:search',
            'nearby': 'photos:search',
            'ocr_search': 'photos:search',
            'semantic_search': 'photos:search',
            'thumbnail': 'photos:preview',
            'report_import': 'reports:import',
            'original_download': 'photos:original',
            'mcp': 'mcp:tools',
            'picker': 'picker:use',
        }
        for cap_key, req_scope in scope_map.items():
            if req_scope not in ctx.scopes:
                effective_caps[cap_key] = False
    if ctx.actor_type != 'user':
        effective_caps['report_import'] = False
        effective_caps['picker'] = False
        effective_caps['mcp'] = False
    if not af.get('location', True) or not af.get('captured', True):
        effective_caps['original_download'] = False
    return {
        'system': {
            'id': str(ctx.system.id),
            'code': ctx.system.code,
            'name': ctx.system.name,
            'owner_unit': ctx.system.owner_unit,
        },
        'actor': {
            'username': ctx.user.username,
            'actor_type': ctx.actor_type,
        },
        'timezone': 'Asia/Taipei',
        'capabilities': effective_caps,
        'allowed_fields': {
            'captured': af['captured'],
            'location': af['location'],
            'tags': af['tags'],
        },
        'limits': {
            'max_results_per_page': ctx.system.max_results_per_page,
            'rate_limit_per_minute': ctx.system.rate_limit_per_minute,
            'max_import_count': ctx.system.max_import_count,
            'max_preview_per_call': ctx.system.max_preview_per_call,
        },
        'authorized_dataset_count': len(ds_ids),
    }


def service_list_datasets(ctx: IntegrationContext) -> dict:
    require_capability(ctx, 'search', 'datasets:read')
    ds_ids = effective_dataset_ids(ctx)
    af = normalize_allowed_fields(ctx.system.allowed_fields)
    counts = {
        row['dataset_id']: row['count']
        for row in Photo.objects.filter(status='submitted', dataset_id__in=ds_ids)
        .values('dataset_id')
        .annotate(count=Count('id'))
    }
    datasets = []
    for ds in Dataset.objects.select_related('item__department').filter(id__in=ds_ids).order_by('id'):
        all_keys = [f.get('key') for f in (ds.item.fields or []) if isinstance(f, dict) and f.get('key')]
        permitted_keys = _allowed_custom_field_keys(af, ds.id, all_keys=all_keys)
        fields = [
            {
                'key': f['key'],
                'label': f.get('label', f['key']),
                'type': f.get('type', 'text'),
                'options': f.get('options', []),
            }
            for f in (ds.item.fields or [])
            if isinstance(f, dict) and f.get('key') in permitted_keys
        ]
        datasets.append({
            'id': ds.id,
            'name': ds.name,
            'department_name': ds.item.department.name,
            'archived': ds.archived,
            'submitted_photo_count': counts.get(ds.id, 0),
            'allowed_custom_fields': fields,
        })
    integration_audit(
        ctx,
        'datasets_list',
        dataset_ids=sorted(ds_ids),
        detail={'count': len(datasets)},
    )
    return {
        'datasets': datasets,
        'capabilities': service_capabilities(ctx)['capabilities'],
        'timezone': 'Asia/Taipei',
    }


def _semantic_coverage(q, matched_id_set=None):
    if matched_id_set is None:
        total = q.count()
        indexed = PhotoEmbedding.objects.filter(
            model_key=SEMANTIC_MODEL_KEY,
            photo_id__in=q.values('id'),
            source_sha=F('photo__sha'),
        )
        ready = indexed.filter(state='ready').count()
        failed = indexed.filter(state='failed').count()
        return {'total': total, 'indexed': ready, 'failed': failed, 'pending': total - ready - failed}
    total = len(matched_id_set)
    ready = 0
    failed = 0
    for pid, state in (
        PhotoEmbedding.objects.filter(
            model_key=SEMANTIC_MODEL_KEY,
            photo_id__in=q.values('id'),
            source_sha=F('photo__sha'),
        )
        .values_list('photo_id', 'state')
        .iterator(chunk_size=1000)
    ):
        if pid not in matched_id_set:
            continue
        if state == 'ready':
            ready += 1
        elif state == 'failed':
            failed += 1
    return {'total': total, 'indexed': ready, 'failed': failed, 'pending': total - ready - failed}


def _ocr_coverage_filtered(q, matched_id_set=None):
    if matched_id_set is None:
        return ocr_coverage(q)
    total = len(matched_id_set)
    ready = 0
    failed = 0
    for pid, state in (
        PhotoOcr.objects.filter(
            model_key=OCR_MODEL_KEY,
            photo_id__in=q.values('id'),
            source_sha=F('photo__sha'),
        )
        .values_list('photo_id', 'state')
        .iterator(chunk_size=1000)
    ):
        if pid not in matched_id_set:
            continue
        if state == 'ready':
            ready += 1
        elif state == 'failed':
            failed += 1
    return {'total': total, 'indexed': ready, 'failed': failed, 'pending': total - ready - failed}


def _ocr_search_filtered(scope_qs, query, matched_id_set, limit):
    if matched_id_set is None:
        return ocr_search(scope_qs, query, limit=limit)
    raw = query.strip()
    try:
        normalized = ocr_normalize_query(raw) if ocr_readiness() else raw
    except OcrUnavailable:
        normalized = raw
    terms = [t.casefold() for t in normalized.split() if t.strip()]
    if not terms:
        terms = [normalized.casefold()]
    records = PhotoOcr.objects.filter(
        model_key=OCR_MODEL_KEY,
        state='ready',
        source_sha=F('photo__sha'),
        photo_id__in=scope_qs.values('id'),
    ).exclude(text='')
    hits = []
    for entry in records.iterator(chunk_size=1000):
        if entry.photo_id not in matched_id_set:
            continue
        haystack = entry.text.casefold()
        if all(term in haystack for term in terms):
            hits.append((entry.photo_id, ocr_snippet(entry.text, terms)))
    hits.sort(key=lambda pair: -pair[0])
    total = len(hits)
    return hits[:limit], total


def service_search_photos(ctx: IntegrationContext, params: dict, include_preview_ticket: bool = False) -> dict:
    if not isinstance(params, dict):
        raise IntegrationError('INVALID_CRITERIA', '搜尋參數須為 JSON 物件', status=422)

    search_id = str(uuid.uuid4())
    mode = str(params.get('mode') or 'general').strip()
    if mode not in ('general', 'nearby', 'ocr', 'semantic'):
        raise IntegrationError('INVALID_CRITERIA', f'不支援的搜尋模式：{mode}', status=422)

    cap_for_mode = {
        'general': 'search',
        'nearby': 'nearby',
        'ocr': 'ocr_search',
        'semantic': 'semantic_search',
    }[mode]
    require_capability(ctx, cap_for_mode, 'photos:search')

    eff_ds = effective_dataset_ids(ctx)
    raw_ds = params.get('datasets')
    if raw_ds is not None:
        if not isinstance(raw_ds, list):
            raise IntegrationError('INVALID_CRITERIA', 'datasets 須為資料集 ID 陣列', status=422)
        try:
            req_ds = [int(x) for x in raw_ds]
        except (TypeError, ValueError) as exc:
            raise IntegrationError('INVALID_CRITERIA', 'datasets 包含無效的識別碼', status=422) from exc
        if req_ds:
            unauthorized = [d for d in req_ds if d not in eff_ds]
            if unauthorized:
                integration_audit(
                    ctx,
                    'photos_search',
                    outcome='denied',
                    error_code='PERMISSION_DENIED',
                    search_id=search_id,
                    detail={'mode': mode, 'reason': 'unauthorized_dataset'},
                )
                raise IntegrationError('PERMISSION_DENIED', '包含未授權的資料集', status=403)
            target_ds = sorted(set(req_ds))
        else:
            target_ds = sorted(eff_ds)
    else:
        target_ds = sorted(eff_ds)

    max_limit = max(1, int(ctx.system.max_results_per_page or 50))
    try:
        limit = int(params.get('limit', min(20, max_limit)))
        offset = int(params.get('offset', 0))
    except (TypeError, ValueError) as exc:
        raise IntegrationError('INVALID_CRITERIA', 'limit 或 offset 格式錯誤', status=422) from exc
    if limit < 1 or limit > max_limit:
        raise IntegrationError(
            'LIMIT_EXCEEDED',
            f'每次回傳上限為 1 至 {max_limit} 筆',
            status=422,
            details={'max_results_per_page': max_limit},
        )
    if offset < 0:
        raise IntegrationError('INVALID_CRITERIA', 'offset 不可小於 0', status=422)

    start_dt, start_meta = _parse_time_bound(params.get('from'), is_end=False)
    end_dt, end_meta = _parse_time_bound(params.get('to'), is_end=True)
    if start_dt and end_dt:
        if end_meta[0] == 'date_inclusive' and start_meta[0] == 'date_inclusive':
            if date.fromisoformat(start_meta[1]) > date.fromisoformat(end_meta[1]):
                raise IntegrationError('INVALID_CRITERIA', '拍攝起日不可晚於迄日', status=422)
        elif start_dt > end_dt:
            raise IntegrationError('INVALID_CRITERIA', '起始時間不可晚於結束時間', status=422)

    bounds = None
    if params.get('bounds') is not None:
        try:
            bounds = _parse_bounds(params.get('bounds'))
        except SearchError as exc:
            raise IntegrationError('INVALID_CRITERIA', str(exc), status=422) from exc

    nearby_spec = params.get('nearby')
    if mode == 'nearby' and not nearby_spec:
        raise IntegrationError('INVALID_CRITERIA', '附近搜尋須提供座標與距離（nearby: {lat, lon, radius_m}）', status=422)
    if nearby_spec is not None:
        require_capability(ctx, 'nearby', 'photos:search')
        if not isinstance(nearby_spec, dict):
            raise IntegrationError('INVALID_CRITERIA', 'nearby 格式錯誤', status=422)
        try:
            n_lat = float(nearby_spec['lat'])
            n_lon = float(nearby_spec['lon'])
            n_radius = float(nearby_spec.get('radius_m', 500))
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrationError('INVALID_CRITERIA', 'nearby 須包含有效 lat、lon、radius_m', status=422) from exc
        if not all(math.isfinite(v) for v in (n_lat, n_lon, n_radius)):
            raise IntegrationError('INVALID_CRITERIA', 'nearby 座標或距離不合法', status=422)
        if not (-90 <= n_lat <= 90 and -180 <= n_lon <= 180 and 10 <= n_radius <= 50000):
            raise IntegrationError('INVALID_CRITERIA', 'nearby 座標超出範圍或距離須為 10 至 50000 公尺', status=422)
        nearby_spec = {'lat': n_lat, 'lon': n_lon, 'radius_m': n_radius}

    af = normalize_allowed_fields(ctx.system.allowed_fields)
    if (bounds or nearby_spec) and not af.get('location', True):
        raise IntegrationError('PERMISSION_DENIED', '此串接系統未獲授權使用位置條件', status=403)

    q = effective_photo_queryset(ctx).filter(dataset_id__in=target_ds)

    text = str(params.get('text') or '').strip()[:200]
    if text:
        q = q.filter(name__icontains=text)

    regions = {}
    for key, col in (('county', 'admin_county'), ('town', 'admin_town'), ('village', 'admin_village')):
        val = str(params.get(key) or '').strip()
        if val:
            if not af.get('location', True):
                raise IntegrationError('PERMISSION_DENIED', '此串接系統未獲授權使用行政區條件', status=403)
            regions[key] = val
            q = q.filter(**{col: val})

    excluded_missing_time = 0
    if start_dt or end_dt:
        if not af.get('captured', True):
            raise IntegrationError('PERMISSION_DENIED', '此串接系統未獲授權使用拍攝時間條件', status=403)
        excluded_missing_time = q.filter(captured__isnull=True).count()
        if start_dt:
            q = q.filter(captured__gte=start_dt)
        if end_dt:
            if end_meta[0] == 'date_inclusive':
                q = q.filter(captured__lt=end_dt)
            else:
                q = q.filter(captured__lte=end_dt)

    if bounds:
        q = _apply_bounds(q, bounds)

    if nearby_spec:
        lat_deg = nearby_spec['radius_m'] / 111000.0
        cos_lat = max(0.05, math.cos(math.radians(nearby_spec['lat'])))
        lon_deg = nearby_spec['radius_m'] / (111000.0 * cos_lat)
        q = q.filter(
            lat__isnull=False,
            lon__isnull=False,
            lat__gte=nearby_spec['lat'] - lat_deg,
            lat__lte=nearby_spec['lat'] + lat_deg,
            lon__gte=nearby_spec['lon'] - lon_deg,
            lon__lte=nearby_spec['lon'] + lon_deg,
        )

    raw_tags = params.get('tags') or []
    if not isinstance(raw_tags, list) or len(raw_tags) > 30:
        raise IntegrationError('INVALID_CRITERIA', 'tags 格式錯誤或超過 30 個', status=422)
    tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    if tags and not af.get('tags', True):
        raise IntegrationError('PERMISSION_DENIED', '此串接系統未獲授權使用標籤條件', status=403)

    field_rules = params.get('fields') or []
    if not isinstance(field_rules, list) or len(field_rules) > 30:
        raise IntegrationError('INVALID_CRITERIA', 'fields 格式錯誤或超過 30 個條件', status=422)
    if field_rules:
        if len(target_ds) != 1:
            raise IntegrationError('INVALID_CRITERIA', '篩選自訂欄位時須指定單一資料集', status=422)
        ds_obj = Dataset.objects.select_related('item').filter(id=target_ds[0]).first()
        all_keys = [f.get('key') for f in (ds_obj.item.fields or []) if isinstance(f, dict) and f.get('key')] if ds_obj else []
        permitted = _allowed_custom_field_keys(af, target_ds[0], all_keys=all_keys)
        for rule in field_rules:
            if not isinstance(rule, dict) or rule.get('key') not in permitted:
                raise IntegrationError('PERMISSION_DENIED', '包含未開放的自訂欄位條件', status=403)

    # Apply python-level filters (tags, custom fields, exact nearby distance) without giant SQL IN clauses
    distance_by_id = {}
    matched_ids = None
    matched_id_set = None
    nearby_sort_rows = []
    if tags or field_rules or nearby_spec:
        matched_ids = []
        for p in q.order_by('-captured', '-id').iterator(chunk_size=1000):
            if tags and not any(t in (p.tags or []) for t in tags):
                continue
            if field_rules and not all(custom_match(p, r) for r in field_rules):
                continue
            if nearby_spec:
                dist = _haversine_meters(nearby_spec['lat'], nearby_spec['lon'], p.lat, p.lon)
                if dist > nearby_spec['radius_m'] + 1e-6:
                    continue
                distance_by_id[p.id] = round(dist, 2)
                nearby_sort_rows.append((distance_by_id[p.id], -(p.captured.timestamp() if p.captured else 0), -p.id, p.id))
            matched_ids.append(p.id)
        matched_id_set = set(matched_ids)

    normalized = {
        'mode': mode,
        'datasets': target_ds,
        'timezone': 'Asia/Taipei',
        'from': start_meta[1] if start_meta else None,
        'to': end_meta[1] if end_meta else None,
        'to_inclusive': bool(end_meta and end_meta[0] == 'date_inclusive'),
        'bounds': bounds,
        'nearby': nearby_spec,
        'regions': regions,
        'tags': tags,
        'text_length': len(text),
        'limit': limit,
        'offset': offset,
    }

    index_status = None
    items = []
    total = 0

    if mode == 'general':
        if matched_ids is not None:
            total = len(matched_ids)
            page_ids = matched_ids[offset : offset + limit]
            by_id = {p.id: p for p in effective_photo_queryset(ctx).filter(id__in=page_ids)}
            rows = [by_id[pid] for pid in page_ids if pid in by_id]
        else:
            total = q.count()
            rows = list(q.order_by('-captured', '-id')[offset : offset + limit])
        items = [
            serialize_integration_photo(
                p,
                ctx,
                allowed_fields=af,
                match_reason={'mode': 'nearby', 'distance_m': distance_by_id[p.id]}
                if p.id in distance_by_id
                else {'mode': 'general'},
                include_preview_ticket=include_preview_ticket,
            )
            for p in rows
        ]
    elif mode == 'nearby':
        nearby_sort_rows.sort()
        total = len(nearby_sort_rows)
        page_ids = [row[3] for row in nearby_sort_rows[offset : offset + limit]]
        by_id = {p.id: p for p in effective_photo_queryset(ctx).filter(id__in=page_ids)}
        page_rows = [by_id[pid] for pid in page_ids if pid in by_id]
        items = [
            serialize_integration_photo(
                p,
                ctx,
                allowed_fields=af,
                match_reason={'mode': 'nearby', 'distance_m': distance_by_id.get(p.id)},
                include_preview_ticket=include_preview_ticket,
            )
            for p in page_rows
        ]
    elif mode == 'ocr':
        query = str(params.get('query') or '').strip()
        if not query or len(query) > 100:
            raise IntegrationError('INVALID_CRITERIA', 'OCR 搜尋須提供 1 至 100 字的關鍵字', status=422)
        cov = _ocr_coverage_filtered(q, matched_id_set=matched_id_set)
        ready_flag = ocr_readiness()
        index_status = {'ready': ready_flag, 'model': OCR_MODEL_KEY, **cov}
        if cov['indexed'] == 0 and not ready_flag:
            integration_audit(
                ctx,
                'photos_search',
                outcome='failed',
                error_code='MODEL_UNAVAILABLE',
                search_id=search_id,
                dataset_ids=target_ds,
                detail={'mode': 'ocr', 'query_length': len(query)},
            )
            raise IntegrationError(
                'MODEL_UNAVAILABLE',
                '離線 OCR 模型目前不可用且授權範圍內無既有 OCR 索引',
                status=503,
                details={'index_status': index_status},
            )
        if cov['indexed'] > 0:
            hits, total = _ocr_search_filtered(q, query, matched_id_set=matched_id_set, limit=max(500, offset + limit))
            page_hits = hits[offset : offset + limit]
            by_id = {p.id: p for p in effective_photo_queryset(ctx).filter(id__in=[pid for pid, _ in page_hits])}
            for pid, snippet in page_hits:
                if pid in by_id:
                    items.append(
                        serialize_integration_photo(
                            by_id[pid],
                            ctx,
                            allowed_fields=af,
                            match_reason={'mode': 'ocr', 'snippet': snippet},
                            include_preview_ticket=include_preview_ticket,
                        )
                    )
        normalized['query_length'] = len(query)
    elif mode == 'semantic':
        query = str(params.get('query') or '').strip()
        if not query or len(query) > 100:
            raise IntegrationError('INVALID_CRITERIA', '影像內容搜尋須提供 1 至 100 字的中文關鍵字或短句', status=422)
        try:
            minimum = float(params.get('minimum', 0.20))
        except (TypeError, ValueError) as exc:
            raise IntegrationError('INVALID_CRITERIA', 'minimum 相近度格式錯誤', status=422) from exc
        if not math.isfinite(minimum) or not (-1.0 <= minimum <= 1.0):
            raise IntegrationError('INVALID_CRITERIA', 'minimum 相近度須介於 -1 至 1', status=422)
        cov = _semantic_coverage(q, matched_id_set=matched_id_set)
        ready_flag = semantic_readiness()
        index_status = {'ready': ready_flag, 'model': SEMANTIC_MODEL_KEY, **cov}
        if not ready_flag:
            integration_audit(
                ctx,
                'photos_search',
                outcome='failed',
                error_code='MODEL_UNAVAILABLE',
                search_id=search_id,
                dataset_ids=target_ds,
                detail={'mode': 'semantic', 'query_length': len(query)},
            )
            raise IntegrationError(
                'MODEL_UNAVAILABLE',
                '離線中文影像內容搜尋模型目前不可用',
                status=503,
                details={'index_status': index_status},
            )
        if cov['indexed'] > 0:
            try:
                vec = text_vector(query)
            except SemanticUnavailable as exc:
                raise IntegrationError(
                    'MODEL_UNAVAILABLE',
                    str(exc),
                    status=503,
                    details={'index_status': index_status},
                ) from exc
            if not valid_vector(vec):
                raise IntegrationError('MODEL_UNAVAILABLE', '模型向量格式無效', status=503)
            embeddings = PhotoEmbedding.objects.filter(
                model_key=SEMANTIC_MODEL_KEY,
                state='ready',
                source_sha=F('photo__sha'),
                photo_id__in=q.values('id'),
            )
            scored = []
            for entry in embeddings.iterator(chunk_size=1000):
                if matched_id_set is not None and entry.photo_id not in matched_id_set:
                    continue
                if not valid_vector(entry.vector):
                    continue
                score = sum(a * b for a, b in zip(vec, entry.vector))
                if score >= minimum:
                    scored.append((entry.photo_id, max(-1.0, min(1.0, score))))
            scored.sort(key=lambda pair: (-pair[1], pair[0]))
            total = len(scored)
            page_hits = scored[offset : offset + limit]
            by_id = {p.id: p for p in effective_photo_queryset(ctx).filter(id__in=[pid for pid, _ in page_hits])}
            for pid, score in page_hits:
                if pid in by_id:
                    items.append(
                        serialize_integration_photo(
                            by_id[pid],
                            ctx,
                            allowed_fields=af,
                            match_reason={'mode': 'semantic', 'score': round(score, 5)},
                            include_preview_ticket=include_preview_ticket,
                        )
                    )
        normalized['query_length'] = len(query)
        normalized['minimum'] = minimum

    has_next = (offset + len(items)) < total
    integration_audit(
        ctx,
        'photos_search',
        search_id=search_id,
        dataset_ids=target_ds,
        photo_ids=[it['photo_id'] for it in items],
        detail={
            'mode': mode,
            'total': total,
            'returned': len(items),
            'offset': offset,
            'limit': limit,
        },
    )
    return {
        'search_id': search_id,
        'normalized_criteria': normalized,
        'total': total,
        'returned': len(items),
        'offset': offset,
        'limit': limit,
        'has_next': has_next,
        'next_offset': (offset + len(items)) if has_next else None,
        'excluded_missing_time': excluded_missing_time,
        'index_status': index_status,
        'items': items,
    }


def service_resolve_photos(
    ctx: IntegrationContext,
    photo_ids,
    expected_versions=None,
    include_preview_ticket: bool = False,
) -> dict:
    require_capability(ctx, 'search', 'photos:search')
    if not isinstance(photo_ids, list) or not (1 <= len(photo_ids) <= 100):
        raise IntegrationError('INVALID_CRITERIA', 'photo_ids 須為 1 至 100 筆照片 ID', status=422)
    try:
        clean_ids = [int(x) for x in photo_ids]
    except (TypeError, ValueError) as exc:
        raise IntegrationError('INVALID_CRITERIA', 'photo_ids 包含無效識別碼', status=422) from exc

    exp_map = {}
    if isinstance(expected_versions, dict):
        for k, v in expected_versions.items():
            try:
                exp_map[int(k)] = int(v)
            except (TypeError, ValueError):
                pass

    af = normalize_allowed_fields(ctx.system.allowed_fields)
    authorized = {p.id: p for p in effective_photo_queryset(ctx).filter(id__in=clean_ids)}
    results = []
    denied_ids = []
    for pid in clean_ids:
        p = authorized.get(pid)
        if not p:
            denied_ids.append(pid)
            results.append({
                'photo_id': pid,
                'status': 'unavailable',
                'error_code': 'PHOTO_UNAVAILABLE',
                'message': '照片不存在、已回收或沒有存取權限',
            })
        else:
            serialized = serialize_integration_photo(
                p,
                ctx,
                allowed_fields=af,
                include_preview_ticket=include_preview_ticket,
            )
            version_changed = pid in exp_map and exp_map[pid] != p.version
            results.append({
                'photo_id': pid,
                'status': 'available',
                'version_changed': version_changed,
                'expected_version': exp_map.get(pid),
                'photo': serialized,
            })

    integration_audit(
        ctx,
        'photos_resolve',
        outcome='partial_failed' if denied_ids and authorized else ('denied' if denied_ids else 'success'),
        photo_ids=clean_ids,
        detail={'authorized_count': len(authorized), 'unavailable_count': len(denied_ids)},
    )
    return {'results': results}


def service_issue_preview_ticket(ctx: IntegrationContext, photo_id: int) -> dict:
    require_capability(ctx, 'thumbnail', 'photos:preview')
    p = effective_photo_queryset(ctx).filter(id=photo_id).first()
    if not p:
        integration_audit(
            ctx,
            'preview_ticket',
            outcome='denied',
            error_code='PERMISSION_DENIED',
            photo_ids=[photo_id],
        )
        raise IntegrationError('PERMISSION_DENIED', '找不到照片或沒有存取權限', status=403)
    ticket_info = issue_image_ticket(ctx, p.id, variant='preview', purpose='preview')
    integration_audit(
        ctx,
        'preview_ticket',
        dataset_ids=[p.dataset_id],
        photo_ids=[p.id],
        detail={'variant': 'preview'},
    )
    return {
        'photo_id': p.id,
        'version': p.version,
        **ticket_info,
    }


def _render_sanitized_jpeg(file_field, max_edge: int, quality: int = 88) -> tuple:
    """Apply EXIF orientation, downscale without upscaling, and strip all embedded EXIF metadata."""
    try:
        with file_field.open('rb') as fh:
            img = Image.open(fh)
            img.load()
            img = ImageOps.exif_transpose(img)
    except (OSError, UnidentifiedImageError) as exc:
        raise IntegrationError('STORAGE_UNAVAILABLE', '照片原檔遺失或無法解碼', status=503) from exc

    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    elif img.mode == 'L':
        img = img.convert('RGB')

    w, h = img.size
    if max(w, h) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    # Save without exif parameter so all GPS/camera EXIF metadata is stripped
    img.save(buf, format='JPEG', quality=quality, optimize=True)
    raw = buf.getvalue()
    sha = hashlib.sha256(raw).hexdigest()
    return raw, sha, len(raw), img.size[0], img.size[1]


def service_get_mcp_previews(ctx: IntegrationContext, photo_ids) -> dict:
    """Return authorized thumbnails as bounded JPEG bytes + structured metadata for MCP."""
    require_capability(ctx, 'thumbnail', 'photos:preview')
    max_call = max(1, int(ctx.system.max_preview_per_call or 12))
    if not isinstance(photo_ids, list) or not (1 <= len(photo_ids) <= max_call):
        raise IntegrationError(
            'LIMIT_EXCEEDED',
            f'單次最多取得 1 至 {max_call} 張預覽縮圖',
            status=422,
            details={'max_preview_per_call': max_call},
        )
    try:
        clean_ids = [int(x) for x in photo_ids]
    except (TypeError, ValueError) as exc:
        raise IntegrationError('INVALID_CRITERIA', 'photo_ids 格式錯誤', status=422) from exc

    af = normalize_allowed_fields(ctx.system.allowed_fields)
    authorized = {p.id: p for p in effective_photo_queryset(ctx).filter(id__in=clean_ids)}
    previews = []
    for pid in clean_ids:
        p = authorized.get(pid)
        if not p:
            previews.append({
                'photo_id': pid,
                'status': 'unavailable',
                'error_code': 'PHOTO_UNAVAILABLE',
                'message': '照片不存在、已回收或沒有存取權限',
            })
            continue
        source_field = p.preview if p.preview else p.file
        if not source_field:
            previews.append({
                'photo_id': pid,
                'status': 'failed',
                'error_code': 'STORAGE_UNAVAILABLE',
                'message': '預覽檔尚未完成',
            })
            continue
        try:
            edge = MCP_PREVIEW_MAX_EDGE
            raw, sha, size, w, h = _render_sanitized_jpeg(source_field, max_edge=edge, quality=78)
            if size > MCP_PREVIEW_MAX_BYTES:
                raw, sha, size, w, h = _render_sanitized_jpeg(source_field, max_edge=420, quality=68)
        except IntegrationError as exc:
            previews.append({
                'photo_id': pid,
                'status': 'failed',
                'error_code': exc.code,
                'message': exc.message,
            })
            continue

        meta = serialize_integration_photo(p, ctx, allowed_fields=af, include_preview_ticket=True)
        previews.append({
            'photo_id': pid,
            'status': 'ready',
            'mime_type': 'image/jpeg',
            'width': w,
            'height': h,
            'size': size,
            'sha256': sha,
            'bytes': raw,
            'photo': meta,
        })

    integration_audit(
        ctx,
        'mcp_previews',
        photo_ids=clean_ids,
        detail={'ready_count': sum(1 for x in previews if x['status'] == 'ready')},
    )
    return {'previews': previews}


def _process_import_item(ctx: IntegrationContext, job: ReportImportJob, item: ReportImportItem, af: dict):
    p = effective_photo_queryset(ctx).filter(id=item.requested_photo_id).first()
    if not p:
        item.state = 'failed'
        item.error_code = 'PHOTO_UNAVAILABLE'
        item.error_message = '照片不存在、已回收或沒有存取權限'
        item.save()
        return

    item.photo = p
    item.dataset_id_snapshot = p.dataset_id
    item.dataset_name_snapshot = p.dataset.name
    item.actual_version = p.version
    item.source_sha = p.sha

    if item.expected_version is not None and int(item.expected_version) != int(p.version):
        item.state = 'failed'
        item.error_code = 'VERSION_CHANGED'
        item.error_message = f'照片版本已由 v{item.expected_version} 變更為 v{p.version}，請重新檢閱'
        item.save()
        return

    if item.variant == 'original' and not can_download_original(ctx, p):
        item.state = 'failed'
        item.error_code = 'PERMISSION_DENIED'
        item.error_message = '沒有原圖下載權限'
        item.save()
        return

    serialized = serialize_integration_photo(p, ctx, allowed_fields=af)
    item.captured_info = {
        'captured': serialized['captured'],
        'timezone': serialized['timezone'],
        'time_confirmed': serialized['time_confirmed'],
        'location': serialized['location'],
        'attrs': serialized['attrs'],
        'quality_warnings': serialized['quality_warnings'],
    }

    if not p.file:
        item.state = 'failed'
        item.error_code = 'STORAGE_UNAVAILABLE'
        item.error_message = '照片來源檔案遺失'
        item.save()
        return

    try:
        if item.variant == 'report':
            raw, sha, size, _, _ = _render_sanitized_jpeg(p.file, max_edge=REPORT_MAX_EDGE, quality=90)
            ext = 'jpg'
            fmt = 'JPEG'
        else:
            with p.file.open('rb') as fh:
                raw = fh.read()
            sha = hashlib.sha256(raw).hexdigest()
            size = len(raw)
            ext = (p.name.rsplit('.', 1)[-1].lower() if '.' in p.name else 'jpg')[:10]
            fmt = ext.upper()
    except (IntegrationError, OSError) as exc:
        item.state = 'failed'
        item.error_code = 'STORAGE_UNAVAILABLE'
        item.error_message = getattr(exc, 'message', '無法讀取照片來源檔案')
        item.save()
        return

    filename = f'report_{job.id}_{item.id}.{ext}'
    item.output_file.save(filename, ContentFile(raw), save=False)
    item.output_sha = sha
    item.output_size = size
    item.output_format = fmt
    item.state = 'ready'
    item.error_code = ''
    item.error_message = ''
    item.save()


def _recompute_job_state(job: ReportImportJob):
    states = list(job.items.values_list('state', flat=True))
    if not states:
        job.state = 'failed'
    elif all(s in ('ready', 'expired') for s in states):
        job.state = 'completed'
    elif any(s in ('ready', 'expired') for s in states):
        job.state = 'partial_failed'
    else:
        job.state = 'failed'
    job.save(update_fields=['state', 'updated'])


def _serialize_import_job(ctx: IntegrationContext, job: ReportImportJob, idempotent_replay=False) -> dict:
    eff_ids = effective_dataset_ids(ctx)
    items_out = []
    for item in job.items.select_related('photo__dataset__item').all():
        live_ok = bool(
            item.state == 'ready'
            and item.photo_id
            and item.photo.status == 'submitted'
            and item.photo.dataset_id in eff_ids
        )
        if item.variant == 'original' and live_ok:
            live_ok = can_download_original(ctx, item.photo)

        download_ticket = None
        live_status = item.state
        revocation_notice = ''
        if item.state == 'ready':
            if live_ok:
                download_ticket = issue_image_ticket(
                    ctx,
                    item.photo_id,
                    variant=item.variant,
                    purpose='report_import',
                    import_item_id=str(item.id),
                )
            else:
                live_status = 'revoked'
                revocation_notice = '照片已回收或授權已撤銷，無法再取用圖片'

        output_info = None
        if item.state == 'ready' and download_ticket:
            output_info = {
                'sha256': item.output_sha,
                'size': item.output_size,
                'format': item.output_format,
                'expired': False,
                'download_ticket': download_ticket,
            }
        elif item.state == 'expired':
            output_info = {
                'sha256': item.output_sha,
                'size': item.output_size,
                'format': item.output_format,
                'expired': True,
                'download_ticket': None,
            }

        items_out.append({
            'item_id': str(item.id),
            'ordinal': item.ordinal,
            'photo_id': item.requested_photo_id,
            'dataset_id': item.dataset_id_snapshot,
            'dataset_name': item.dataset_name_snapshot,
            'expected_version': item.expected_version,
            'actual_version': item.actual_version,
            'source_sha': item.source_sha,
            'variant': item.variant,
            'caption': item.caption,
            'captured_info': item.captured_info,
            'state': live_status,
            'error_code': item.error_code if live_status != 'revoked' else 'PERMISSION_DENIED',
            'error_message': revocation_notice or item.error_message,
            'output': output_info,
            'fetched_at': item.fetched_at.isoformat() if item.fetched_at else None,
        })

    ready_count = sum(1 for x in items_out if x['state'] == 'ready')
    expired_count = sum(1 for x in items_out if x['state'] == 'expired')
    failed_count = len(items_out) - ready_count - expired_count
    return {
        'import_job_id': str(job.id),
        'idempotency_key': job.idempotency_key,
        'idempotent_replay': idempotent_replay,
        'system_code': job.system.code,
        'actor_username': job.user.username,
        'case_id': job.case_id,
        'report_id': job.report_id,
        'state': job.state,
        'confirmed_at': job.confirmed_at.isoformat(),
        'summary': {
            'total': len(items_out),
            'ready': ready_count,
            'expired': expired_count,
            'failed': failed_count,
        },
        'items': items_out,
    }


def service_create_report_import(ctx: IntegrationContext, data: dict, max_count_override: int = None) -> dict:
    if ctx.actor_type != 'user':
        raise IntegrationError(
            'PERMISSION_DENIED',
            '正式報告匯入須經互動式操作人員確認，不可使用服務身分憑證',
            status=403,
        )
    require_capability(ctx, 'report_import', 'reports:import')
    if not isinstance(data, dict):
        raise IntegrationError('INVALID_CRITERIA', '請求格式錯誤', status=422)

    idem_key = str(data.get('idempotency_key') or '').strip()
    if not idem_key or len(idem_key) > 128:
        raise IntegrationError('INVALID_CRITERIA', '請提供 1 至 128 字的 idempotency_key', status=422)

    existing = ReportImportJob.objects.filter(system=ctx.system, idempotency_key=idem_key).first()
    if existing:
        if existing.user_id != ctx.user.id:
            raise IntegrationError('PERMISSION_DENIED', '此冪等識別碼屬於其他操作人員', status=403)
        return _serialize_import_job(ctx, existing, idempotent_replay=True)

    raw_items = data.get('items')
    max_count = max(1, int(ctx.system.max_import_count or 50))
    if max_count_override is not None:
        max_count = max(1, min(max_count, int(max_count_override)))
    if not isinstance(raw_items, list) or not (1 <= len(raw_items) <= max_count):
        raise IntegrationError(
            'LIMIT_EXCEEDED',
            f'單次匯入張數須為 1 至 {max_count} 張',
            status=422,
            details={'max_import_count': max_count},
        )

    case_id = str(data.get('case_id') or '').strip()[:120]
    report_id = str(data.get('report_id') or '').strip()[:120]
    parsed_items = []
    for idx, entry in enumerate(raw_items):
        if not isinstance(entry, dict) or 'photo_id' not in entry:
            raise IntegrationError('INVALID_CRITERIA', f'第 {idx + 1} 筆照片格式錯誤', status=422)
        try:
            pid = int(entry['photo_id'])
            exp_ver = int(entry['expected_version']) if entry.get('expected_version') is not None else None
            ordinal = int(entry.get('ordinal', idx + 1))
        except (TypeError, ValueError) as exc:
            raise IntegrationError('INVALID_CRITERIA', f'第 {idx + 1} 筆照片識別或版本格式錯誤', status=422) from exc
        variant = str(entry.get('variant') or 'report').strip()
        if variant not in ('report', 'original'):
            raise IntegrationError('INVALID_CRITERIA', 'variant 僅支援 report 或 original', status=422)
        caption = str(entry.get('caption') or '')[:500]
        parsed_items.append({
            'photo_id': pid,
            'expected_version': exp_ver,
            'ordinal': ordinal,
            'variant': variant,
            'caption': caption,
        })

    af = normalize_allowed_fields(ctx.system.allowed_fields)
    try:
        with transaction.atomic():
            job = ReportImportJob.objects.create(
                system=ctx.system,
                user=ctx.user,
                idempotency_key=idem_key,
                case_id=case_id,
                report_id=report_id,
                state='pending',
                request_payload={'item_count': len(parsed_items)},
                confirmed_at=timezone.now(),
            )
            for spec in parsed_items:
                item = ReportImportItem.objects.create(
                    job=job,
                    ordinal=spec['ordinal'],
                    requested_photo_id=spec['photo_id'],
                    expected_version=spec['expected_version'],
                    variant=spec['variant'],
                    caption=spec['caption'],
                    state='pending',
                )
                _process_import_item(ctx, job, item, af)
            _recompute_job_state(job)
    except IntegrityError:
        existing = ReportImportJob.objects.get(system=ctx.system, idempotency_key=idem_key)
        if existing.user_id != ctx.user.id:
            raise IntegrationError('PERMISSION_DENIED', '此冪等識別碼屬於其他操作人員', status=403)
        return _serialize_import_job(ctx, existing, idempotent_replay=True)

    integration_audit(
        ctx,
        'report_import_create',
        outcome='success' if job.state == 'completed' else job.state,
        photo_ids=[s['photo_id'] for s in parsed_items],
        case_id=case_id,
        report_id=report_id,
        import_job_id=str(job.id),
        detail={'state': job.state, 'count': len(parsed_items)},
    )
    return _serialize_import_job(ctx, job, idempotent_replay=False)


def service_get_report_import(ctx: IntegrationContext, job_id: str) -> dict:
    require_capability(ctx, 'report_import', 'reports:import')
    try:
        uid = uuid.UUID(str(job_id))
    except (ValueError, TypeError) as exc:
        raise IntegrationError('INVALID_CRITERIA', '匯入作業識別碼格式錯誤', status=422) from exc
    job = ReportImportJob.objects.select_related('system', 'user').filter(id=uid, system=ctx.system).first()
    if not job or (ctx.actor_type == 'user' and job.user_id != ctx.user.id):
        raise IntegrationError('PERMISSION_DENIED', '找不到匯入作業或沒有權限', status=404)
    return _serialize_import_job(ctx, job, idempotent_replay=False)


def service_retry_report_import(ctx: IntegrationContext, job_id: str) -> dict:
    """Safely retry only failed items in a partial_failed/failed job without duplicating ready items."""
    require_capability(ctx, 'report_import', 'reports:import')
    try:
        uid = uuid.UUID(str(job_id))
    except (ValueError, TypeError) as exc:
        raise IntegrationError('INVALID_CRITERIA', '匯入作業識別碼格式錯誤', status=422) from exc
    job = ReportImportJob.objects.select_related('system', 'user').filter(id=uid, system=ctx.system).first()
    if not job or (ctx.actor_type == 'user' and job.user_id != ctx.user.id):
        raise IntegrationError('PERMISSION_DENIED', '找不到匯入作業或沒有權限', status=404)

    af = normalize_allowed_fields(ctx.system.allowed_fields)
    retried_ids = []
    with transaction.atomic():
        for item in job.items.select_for_update().filter(state='failed'):
            retried_ids.append(item.requested_photo_id)
            _process_import_item(ctx, job, item, af)
        _recompute_job_state(job)

    integration_audit(
        ctx,
        'report_import_retry',
        outcome='success' if job.state == 'completed' else job.state,
        photo_ids=retried_ids,
        case_id=job.case_id,
        report_id=job.report_id,
        import_job_id=str(job.id),
        detail={'retried_count': len(retried_ids), 'state': job.state},
    )
    return _serialize_import_job(ctx, job, idempotent_replay=False)


def service_serve_asset_by_ticket(ticket: str, expected_system_id: str = None, expected_photo_id: int = None) -> tuple:
    """Serve controlled image bytes after live re-authorization check."""
    ctx, payload = verify_image_ticket(
        ticket,
        expected_system_id=expected_system_id,
        expected_photo_id=expected_photo_id,
    )
    pid = int(payload['pid'])
    variant = str(payload.get('v') or 'preview')
    iid = str(payload.get('iid') or '')

    p = effective_photo_queryset(ctx).filter(id=pid).first()
    if not p:
        integration_audit(
            ctx,
            'asset_download',
            outcome='denied',
            error_code='PERMISSION_DENIED',
            photo_ids=[pid],
            detail={'variant': variant, 'reason': 'revoked_or_recycled'},
        )
        raise IntegrationError('PERMISSION_DENIED', '照片已回收或目前沒有存取權限', status=403)

    if variant == 'preview':
        if not can_preview(ctx, p):
            raise IntegrationError('PERMISSION_DENIED', '沒有預覽縮圖權限', status=403)
        source_field = p.preview if p.preview else p.file
        if not source_field:
            raise IntegrationError('STORAGE_UNAVAILABLE', '預覽檔案不存在', status=503)
        raw, sha, size, _, _ = _render_sanitized_jpeg(source_field, max_edge=1200, quality=85)
        integration_audit(ctx, 'asset_download', dataset_ids=[p.dataset_id], photo_ids=[p.id], detail={'variant': 'preview'})
        return raw, 'image/jpeg', f'preview_{p.id}.jpg', sha, size

    if variant == 'report':
        if not can_report_import(ctx, p) or not iid:
            raise IntegrationError('PERMISSION_DENIED', '沒有報告取圖權限或憑證不符', status=403)
        item_qs = ReportImportItem.objects.select_related('job').filter(
            id=iid,
            photo_id=p.id,
            job__system=ctx.system,
        )
        if ctx.actor_type == 'user':
            item_qs = item_qs.filter(job__user=ctx.user)
        item = item_qs.first()
        if item and item.state == 'expired':
            raise IntegrationError('FILE_EXPIRED', '此報告用圖副本已逾保留期限並依政策清理，請重新建立報告匯入作業', status=410)
        if not item or item.state != 'ready' or not item.output_file:
            raise IntegrationError('STORAGE_UNAVAILABLE', '報告用圖副本不存在', status=404)
        try:
            with item.output_file.open('rb') as fh:
                raw = fh.read()
        except OSError as exc:
            raise IntegrationError('STORAGE_UNAVAILABLE', '報告用圖檔案暫不可用', status=503) from exc
        sha = hashlib.sha256(raw).hexdigest()
        if item.output_sha and sha != item.output_sha:
            raise IntegrationError('STORAGE_UNAVAILABLE', '檔案雜湊驗證失敗', status=503)
        item.fetched_at = timezone.now()
        item.save(update_fields=['fetched_at', 'updated'])
        integration_audit(
            ctx,
            'asset_download',
            dataset_ids=[p.dataset_id],
            photo_ids=[p.id],
            case_id=item.job.case_id,
            report_id=item.job.report_id,
            import_job_id=str(item.job_id),
            detail={'variant': 'report', 'size': len(raw)},
        )
        return raw, 'image/jpeg', f'report_{p.id}.jpg', sha, len(raw)

    if variant == 'original':
        if not can_download_original(ctx, p):
            raise IntegrationError('PERMISSION_DENIED', '沒有原圖下載權限', status=403)
        if iid:
            item_qs = ReportImportItem.objects.select_related('job').filter(
                id=iid,
                photo_id=p.id,
                job__system=ctx.system,
                variant='original',
            )
            if ctx.actor_type == 'user':
                item_qs = item_qs.filter(job__user=ctx.user)
            item = item_qs.first()
            if item and item.state == 'expired':
                raise IntegrationError('FILE_EXPIRED', '此報告原圖副本已逾保留期限並依政策清理，請重新建立報告匯入作業', status=410)
            if not item or item.state != 'ready' or not item.output_file:
                raise IntegrationError('STORAGE_UNAVAILABLE', '報告原圖副本不存在', status=404)
            try:
                with item.output_file.open('rb') as fh:
                    raw = fh.read()
            except OSError as exc:
                raise IntegrationError('STORAGE_UNAVAILABLE', '原圖檔案暫不可用', status=503) from exc
            sha = hashlib.sha256(raw).hexdigest()
            if item.output_sha and sha != item.output_sha:
                raise IntegrationError('STORAGE_UNAVAILABLE', '檔案雜湊驗證失敗', status=503)
            item.fetched_at = timezone.now()
            item.save(update_fields=['fetched_at', 'updated'])
            integration_audit(
                ctx,
                'asset_download',
                dataset_ids=[p.dataset_id],
                photo_ids=[p.id],
                case_id=item.job.case_id,
                report_id=item.job.report_id,
                import_job_id=str(item.job_id),
                detail={'variant': 'original', 'size': len(raw)},
            )
            return raw, 'application/octet-stream', p.name, sha, len(raw)
        if not p.file:
            raise IntegrationError('STORAGE_UNAVAILABLE', '原圖檔案不存在', status=503)
        try:
            with p.file.open('rb') as fh:
                raw = fh.read()
        except OSError as exc:
            raise IntegrationError('STORAGE_UNAVAILABLE', '原圖暫不可用', status=503) from exc
        sha = hashlib.sha256(raw).hexdigest()
        integration_audit(ctx, 'asset_download', dataset_ids=[p.dataset_id], photo_ids=[p.id], detail={'variant': 'original', 'size': len(raw)})
        return raw, 'application/octet-stream', p.name, sha, len(raw)

    raise IntegrationError('PERMISSION_DENIED', '不支援的圖片種類', status=403)
