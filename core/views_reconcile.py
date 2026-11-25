"""Admin API endpoints for file reconciliation and storage capacity management."""
from django.http import JsonResponse
from .helpers import Problem
from .models import StorageReconcileReport
from .reconcile import (
    compute_capacity_report,
    confirm_orphan_cleanup,
    repair_missing_previews,
    run_reconciliation,
)


def handle_reconcile_api(path, data, u):
    if not u.is_superuser:
        raise Problem('維運對帳與容量統計僅限系統管理者查看與操作', 403)

    if path == 'reconcile/capacity':
        return JsonResponse(compute_capacity_report())

    if path == 'reconcile/reports':
        reports = [
            {
                'id': str(r.id),
                'actor': r.actor.username if r.actor else '系統',
                'mode': r.mode,
                'grace_hours': r.grace_hours,
                'summary': r.summary,
                'issues': r.issues,
                'created': r.created.isoformat(),
            }
            for r in StorageReconcileReport.objects.select_related('actor').order_by('-created')[:20]
        ]
        return JsonResponse({'reports': reports})

    if path == 'reconcile/run':
        grace_hours = int(data.get('grace_hours', 24))
        verify_hashes = bool(data.get('verify_hashes', True))
        report = run_reconciliation(u, grace_hours=grace_hours, verify_hashes=verify_hashes)
        return JsonResponse({
            'ok': True,
            'report': {
                'id': str(report.id),
                'grace_hours': report.grace_hours,
                'summary': report.summary,
                'issues': report.issues,
                'created': report.created.isoformat(),
            },
        })

    if path == 'reconcile/repair_previews':
        photo_ids = data.get('photos')
        res = repair_missing_previews(u, photo_ids=photo_ids)
        return JsonResponse({'ok': True, **res})

    if path == 'reconcile/confirm_orphan_cleanup':
        res = confirm_orphan_cleanup(
            u,
            data.get('paths', []),
            data.get('reason', ''),
            report_id=data.get('report_id'),
            grace_hours=int(data.get('grace_hours', 24)),
        )
        return JsonResponse({'ok': True, **res})

    raise Problem('找不到對帳功能', 404)
