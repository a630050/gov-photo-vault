"""Lifecycle maintenance, retention cleanup, and storage statistics for Integration API / MCP / Picker."""
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import (
    IntegrationAudit,
    IntegrationAuthCode,
    IntegrationSystem,
    IntegrationToken,
    PickerSession,
    ReportImportItem,
    ReportImportJob,
)

DEFAULT_REPORT_RETENTION_DAYS = 30
DEFAULT_TOKEN_RETENTION_DAYS = 30
DEFAULT_AUDIT_DETAIL_DAYS = 180
SENSITIVE_AUDIT_DETAIL_KEYS = ('query', 'text', 'criteria', 'excerpt', 'ocr_excerpt')


def _validate_days(name: str, value) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} 須為整數天數') from exc
    if days < 0 or days > 3650:
        raise ValueError(f'{name} 須介於 0 至 3650 天之間')
    return days


def _measure_report_storage() -> tuple[int, int]:
    """Count files and total bytes under MEDIA_ROOT/integration_reports."""
    root = Path(settings.MEDIA_ROOT) / 'integration_reports'
    if not root.exists():
        return 0, 0
    file_count = 0
    total_bytes = 0
    for entry in root.rglob('*'):
        if entry.is_file():
            file_count += 1
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                pass
    return file_count, total_bytes


def integration_storage_stats(
    report_retention_days: int = DEFAULT_REPORT_RETENTION_DAYS,
    token_retention_days: int = DEFAULT_TOKEN_RETENTION_DAYS,
    audit_detail_days: int = DEFAULT_AUDIT_DETAIL_DAYS,
) -> dict:
    report_days = _validate_days('report_retention_days', report_retention_days)
    token_days = _validate_days('token_retention_days', token_retention_days)
    audit_days = _validate_days('audit_detail_days', audit_detail_days)

    now = timezone.now()
    report_cutoff = now - timedelta(days=report_days)
    token_cutoff = now - timedelta(days=token_days)
    audit_cutoff = now - timedelta(days=audit_days)

    file_count, total_bytes = _measure_report_storage()

    expirable_report_items = ReportImportItem.objects.filter(
        state='ready',
        job__state__in=['completed', 'partial_failed', 'failed'],
        job__updated__lt=report_cutoff,
    ).exclude(output_file='').count()

    expirable_auth_codes = IntegrationAuthCode.objects.filter(
        Q(expires_at__lt=now) | Q(used=True, created__lt=now - timedelta(days=1))
    ).count()

    expirable_tokens = IntegrationToken.objects.filter(
        Q(expires_at__lt=token_cutoff) | Q(revoked=True, created__lt=token_cutoff)
    ).count()

    expirable_picker_sessions = PickerSession.objects.filter(
        Q(expires_at__lt=token_cutoff) | Q(revoked=True, created__lt=token_cutoff)
    ).count()

    redactable_audits = 0
    for detail in IntegrationAudit.objects.filter(created__lt=audit_cutoff).values_list('detail', flat=True).iterator(chunk_size=500):
        if isinstance(detail, dict) and not detail.get('_redacted') and any(k in detail for k in SENSITIVE_AUDIT_DETAIL_KEYS):
            redactable_audits += 1

    return {
        'systems_total': IntegrationSystem.objects.count(),
        'systems_active': IntegrationSystem.objects.filter(active=True, revoked=False).count(),
        'tokens_active': IntegrationToken.objects.filter(revoked=False, expires_at__gt=now).count(),
        'picker_sessions_active': PickerSession.objects.filter(revoked=False, expires_at__gt=now).count(),
        'report_jobs_total': ReportImportJob.objects.count(),
        'report_items_ready': ReportImportItem.objects.filter(state='ready').count(),
        'report_items_expired': ReportImportItem.objects.filter(state='expired').count(),
        'report_storage_files': file_count,
        'report_storage_bytes': total_bytes,
        'cleanup_candidates': {
            'expired_report_files': expirable_report_items,
            'expired_auth_codes': expirable_auth_codes,
            'expired_tokens': expirable_tokens,
            'expired_picker_sessions': expirable_picker_sessions,
            'redactable_audits': redactable_audits,
        },
        'retention_policy': {
            'report_retention_days': report_days,
            'token_retention_days': token_days,
            'audit_detail_days': audit_days,
        },
    }


def run_integration_cleanup(
    report_retention_days: int = DEFAULT_REPORT_RETENTION_DAYS,
    token_retention_days: int = DEFAULT_TOKEN_RETENTION_DAYS,
    audit_detail_days: int = DEFAULT_AUDIT_DETAIL_DAYS,
    dry_run: bool = False,
) -> dict:
    """Clean expired report image copies, stale tokens/sessions/auth codes, and redact aged audit query text."""
    report_days = _validate_days('report_retention_days', report_retention_days)
    token_days = _validate_days('token_retention_days', token_retention_days)
    audit_days = _validate_days('audit_detail_days', audit_detail_days)

    now = timezone.now()
    report_cutoff = now - timedelta(days=report_days)
    token_cutoff = now - timedelta(days=token_days)
    audit_cutoff = now - timedelta(days=audit_days)

    # 1. Expired report image copies (preserve DB provenance hash/size/metadata, delete physical copy)
    report_items_qs = (
        ReportImportItem.objects.select_related('job')
        .filter(
            state='ready',
            job__state__in=['completed', 'partial_failed', 'failed'],
            job__updated__lt=report_cutoff,
        )
        .exclude(output_file='')
    )
    expired_report_files = 0
    failed_report_files = 0
    freed_bytes = 0
    for item in report_items_qs.iterator(chunk_size=100):
        file_name = item.output_file.name
        size_hint = int(item.output_size or 0)
        if not dry_run:
            if file_name:
                try:
                    if item.output_file.storage.exists(file_name):
                        try:
                            size_hint = item.output_file.storage.size(file_name)
                        except OSError:
                            pass
                        item.output_file.storage.delete(file_name)
                except OSError:
                    # Keep DB reference intact so a future cleanup run can retry deleting the physical file.
                    failed_report_files += 1
                    continue
            item.output_file = ''
            item.state = 'expired'
            item.error_code = 'FILE_EXPIRED'
            item.error_message = f'已逾報告副本保留期限（{report_days} 天）並依政策清理實體檔案'
            item.save(update_fields=['output_file', 'state', 'error_code', 'error_message', 'updated'])
        expired_report_files += 1
        freed_bytes += size_hint

    # 2. Expired or used auth codes
    auth_codes_qs = IntegrationAuthCode.objects.filter(
        Q(expires_at__lt=now) | Q(used=True, created__lt=now - timedelta(days=1))
    )
    if dry_run:
        deleted_auth_codes = auth_codes_qs.count()
    else:
        deleted_auth_codes, _ = auth_codes_qs.delete()

    # 3. Aged expired/revoked tokens & picker sessions
    tokens_qs = IntegrationToken.objects.filter(
        Q(expires_at__lt=token_cutoff) | Q(revoked=True, created__lt=token_cutoff)
    )
    if dry_run:
        deleted_tokens = tokens_qs.count()
    else:
        deleted_tokens, _ = tokens_qs.delete()

    sessions_qs = PickerSession.objects.filter(
        Q(expires_at__lt=token_cutoff) | Q(revoked=True, created__lt=token_cutoff)
    )
    if dry_run:
        deleted_picker_sessions = sessions_qs.count()
    else:
        deleted_picker_sessions, _ = sessions_qs.delete()

    # 4. Aged audit query/OCR detail redaction (keep structural provenance fields intact)
    redacted_audits = 0
    for audit_row in IntegrationAudit.objects.filter(created__lt=audit_cutoff).iterator(chunk_size=200):
        detail = audit_row.detail
        if not isinstance(detail, dict) or detail.get('_redacted'):
            continue
        matched_keys = [k for k in SENSITIVE_AUDIT_DETAIL_KEYS if k in detail]
        if not matched_keys:
            continue
        redacted_audits += 1
        if not dry_run:
            new_detail = dict(detail)
            for k in matched_keys:
                new_detail[k] = '[redacted-by-retention-policy]'
            new_detail['_redacted'] = True
            new_detail['_redacted_at'] = now.isoformat()
            with transaction.atomic():
                audit_row.detail = new_detail
                audit_row.save(update_fields=['detail'])

    return {
        'dry_run': bool(dry_run),
        'expired_report_files': expired_report_files,
        'failed_report_files': failed_report_files,
        'freed_bytes': freed_bytes,
        'deleted_auth_codes': deleted_auth_codes,
        'deleted_tokens': deleted_tokens,
        'deleted_picker_sessions': deleted_picker_sessions,
        'redacted_audits': redacted_audits,
        'retention_policy': {
            'report_retention_days': report_days,
            'token_retention_days': token_days,
            'audit_detail_days': audit_days,
        },
        'executed_at': now.isoformat(),
    }
