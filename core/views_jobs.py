"""API endpoints for the unified background job center."""
from django.http import JsonResponse
from .helpers import Problem
from .jobs import _job_scope_queryset, can_view_job, cancel_job, create_job, retry_job, serialize_job
from .models import BackgroundJob


def handle_jobs_api(path, data, u):
    if path == 'jobs/list':
        limit = int(data.get('limit', 50))
        offset = int(data.get('offset', 0))
        if not 1 <= limit <= 200 or offset < 0:
            raise Problem('分頁參數無效', 422)
        q = _job_scope_queryset(u)
        if data.get('kind'):
            q = q.filter(kind=str(data['kind']))
        if data.get('state'):
            q = q.filter(state=str(data['state']))
        total = q.count()
        rows = list(q.order_by('-created')[offset : offset + limit])
        has_next = (offset + len(rows)) < total
        return JsonResponse({
            'jobs': [serialize_job(j) for j in rows],
            'total': total,
            'offset': offset,
            'limit': limit,
            'has_next': has_next,
            'next_offset': (offset + len(rows)) if has_next else None,
        })

    if path == 'jobs/detail':
        job = BackgroundJob.objects.select_related('actor', 'dataset__item').filter(id=data['job']).first()
        if not job:
            raise Problem('找不到工作紀錄', 404)
        if not can_view_job(u, job):
            raise Problem('沒有權限查看此工作', 403)
        return JsonResponse({'job': serialize_job(job)})

    if path == 'jobs/create':
        job = create_job(u, data.get('kind', ''), data)
        return JsonResponse({'ok': True, 'job': serialize_job(job)})

    if path == 'jobs/cancel':
        job = cancel_job(u, data['job'])
        return JsonResponse({'ok': True, 'job': serialize_job(job)})

    if path == 'jobs/retry':
        job = retry_job(u, data['job'])
        return JsonResponse({'ok': True, 'job': serialize_job(job)})

    raise Problem('找不到工作中心功能', 404)
