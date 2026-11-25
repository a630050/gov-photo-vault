from django.db import transaction
from django.db.models import F
from .models import Photo, PhotoOcr
from .ocr_engine import MODEL_KEY, ModelUnavailable, normalize_query, ocr_images, readiness


def pending_ocr_photos(q):
    done = PhotoOcr.objects.filter(model_key=MODEL_KEY, source_sha=F('photo__sha')).values('photo_id')
    return q.filter(status='submitted').exclude(id__in=done)


def index_ocr_batch(limit=5, retry=False, dataset_ids=None):
    q = Photo.objects.filter(status='submitted').order_by('id')
    if dataset_ids:
        q = q.filter(dataset_id__in=dataset_ids)
    if retry:
        failed_q = PhotoOcr.objects.filter(model_key=MODEL_KEY, state='failed')
        if dataset_ids:
            failed_q = failed_q.filter(photo__dataset_id__in=dataset_ids)
        failed_q.delete()
    rows = list(pending_ocr_photos(q)[:limit])
    if not rows:
        return 0
    results = {r['id']: r for r in ocr_images(rows)}
    with transaction.atomic():
        for p in rows:
            if not Photo.objects.filter(id=p.id, sha=p.sha, status='submitted').exists():
                continue
            result = results.get(p.id, {})
            ok = 'error' not in result
            PhotoOcr.objects.update_or_create(
                photo=p,
                model_key=MODEL_KEY,
                defaults={
                    'source_sha': p.sha,
                    'text': result.get('text', '') if ok else '',
                    'state': 'ready' if ok else 'failed',
                    'error': '' if ok else result.get('error', 'OCR 辨識失敗'),
                },
            )
    return len(rows)


def ocr_coverage(q):
    total = q.count()
    indexed = PhotoOcr.objects.filter(model_key=MODEL_KEY, photo_id__in=q.values('id'), source_sha=F('photo__sha'))
    ready = indexed.filter(state='ready').count()
    failed = indexed.filter(state='failed').count()
    return {'total': total, 'indexed': ready, 'failed': failed, 'pending': total - ready - failed}


def _snippet(text, terms):
    flat = ' / '.join(line.strip() for line in text.splitlines() if line.strip())
    if not flat:
        return ''
    lower = flat.casefold()
    pos = -1
    for term in terms:
        idx = lower.find(term)
        if idx >= 0 and (pos < 0 or idx < pos):
            pos = idx
    if pos < 0:
        return flat[:120]
    start = max(0, pos - 24)
    end = min(len(flat), start + 120)
    prefix = '…' if start > 0 else ''
    suffix = '…' if end < len(flat) else ''
    return prefix + flat[start:end] + suffix


def ocr_search(scope_qs, query, limit=500):
    raw = query.strip()
    try:
        normalized = normalize_query(raw) if readiness() else raw
    except ModelUnavailable:
        normalized = raw
    terms = [t.casefold() for t in normalized.split() if t.strip()]
    if not terms:
        terms = [normalized.casefold()]

    records = PhotoOcr.objects.filter(
        model_key=MODEL_KEY,
        state='ready',
        source_sha=F('photo__sha'),
        photo_id__in=scope_qs.values('id'),
    ).exclude(text='')

    hits = []
    for entry in records.iterator():
        haystack = entry.text.casefold()
        if all(term in haystack for term in terms):
            hits.append((entry.photo_id, _snippet(entry.text, terms)))

    hits.sort(key=lambda pair: -pair[0])
    total = len(hits)
    return hits[:limit], total
