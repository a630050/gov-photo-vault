"""Dual-layer authorization, OAuth/OIDC token verification, signed media tickets, and audit helpers."""
from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import secrets
import threading
import time
from datetime import timedelta
from django.core import signing
from django.utils import timezone
from .helpers import dept_ids
from .models import (
    Dataset,
    IntegrationAudit,
    IntegrationSystem,
    IntegrationToken,
    Photo,
    PickerSession,
)
from .permissions import allowed

INTEGRATION_CAPABILITIES = (
    'search',
    'nearby',
    'ocr_search',
    'semantic_search',
    'thumbnail',
    'report_import',
    'original_download',
    'mcp',
    'picker',
)

DEFAULT_CAPABILITIES = {
    'search': True,
    'nearby': True,
    'ocr_search': True,
    'semantic_search': True,
    'thumbnail': True,
    'report_import': True,
    'original_download': False,
    'mcp': True,
    'picker': True,
}

DEFAULT_ALLOWED_FIELDS = {
    'captured': True,
    'location': True,
    'tags': True,
    'custom_fields': {},
}

INTEGRATION_SCOPES = (
    'datasets:read',
    'photos:search',
    'photos:preview',
    'reports:import',
    'photos:original',
    'mcp:tools',
    'picker:use',
)

SERVICE_ALLOWED_SCOPES = (
    'datasets:read',
    'photos:search',
    'photos:preview',
)

IMAGE_TICKET_SALT = 'integration-image-ticket-v1'
DEFAULT_TICKET_TTL = 300
MAX_TICKET_TTL = 900

_rate_lock = threading.Lock()
_rate_buckets = defaultdict(deque)


class IntegrationError(Exception):
    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_dict(self):
        return {
            'error': {
                'code': self.code,
                'message': self.message,
                'details': self.details,
            }
        }


@dataclass
class IntegrationContext:
    system: IntegrationSystem
    user: object
    actor_type: str = 'user'
    channel: str = 'api'
    scopes: tuple | None = None
    audience: str = 'integration-api'
    token_id: str = ''
    picker_session_id: str = ''


def hash_secret(raw: str) -> str:
    return hashlib.sha256(str(raw).encode('utf-8')).hexdigest()


def generate_opaque_token(prefix='itk') -> str:
    return f'{prefix}_{secrets.token_urlsafe(32)}'


def clear_rate_limits():
    with _rate_lock:
        _rate_buckets.clear()


def check_rate_limit(system: IntegrationSystem):
    limit = max(1, int(system.rate_limit_per_minute or 60))
    now = time.monotonic()
    cutoff = now - 60.0
    key = str(system.id)
    with _rate_lock:
        bucket = _rate_buckets[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            raise IntegrationError(
                'RATE_LIMITED',
                '已超過此串接系統每分鐘呼叫頻率上限，請稍後再試',
                status=429,
                details={'rate_limit_per_minute': limit},
            )
        bucket.append(now)


def normalize_capabilities(raw):
    caps = dict(DEFAULT_CAPABILITIES)
    if isinstance(raw, dict):
        for key in INTEGRATION_CAPABILITIES:
            if key in raw:
                caps[key] = bool(raw[key])
    return caps


def normalize_allowed_fields(raw):
    fields = {
        'captured': True,
        'location': True,
        'tags': True,
        'custom_fields': {},
    }
    if isinstance(raw, dict):
        for key in ('captured', 'location', 'tags'):
            if key in raw:
                fields[key] = bool(raw[key])
        cf = raw.get('custom_fields')
        if isinstance(cf, dict):
            clean_cf = {}
            for ds_key, allowed_keys in cf.items():
                if isinstance(allowed_keys, list):
                    clean_cf[str(ds_key)] = [str(k) for k in allowed_keys if str(k).strip()]
            fields['custom_fields'] = clean_cf
    return fields


def validate_system_active(system: IntegrationSystem):
    now = timezone.now()
    if not system or not system.active or system.revoked:
        raise IntegrationError('PERMISSION_DENIED', '串接系統已停用或撤銷授權', status=403)
    if system.valid_from and now < system.valid_from:
        raise IntegrationError('PERMISSION_DENIED', '串接系統尚未到啟用時間', status=403)
    if system.valid_until and now > system.valid_until:
        raise IntegrationError('PERMISSION_DENIED', '串接系統授權已到期', status=403)


def validate_user_active(user):
    if not user or not getattr(user, 'is_active', False):
        raise IntegrationError('PERMISSION_DENIED', '操作人員帳號不存在或已停用', status=403)


def require_capability(ctx: IntegrationContext, capability: str, required_scope: str = None):
    caps = normalize_capabilities(ctx.system.capabilities)
    if not caps.get(capability, False):
        raise IntegrationError(
            'PERMISSION_DENIED',
            f'此串接系統未獲授權使用 {capability} 功能',
            status=403,
            details={'capability': capability},
        )
    if required_scope and ctx.scopes is not None and required_scope not in ctx.scopes:
        raise IntegrationError(
            'PERMISSION_DENIED',
            f'憑證未包含必要權限範圍 {required_scope}',
            status=403,
            details={'scope': required_scope},
        )


def effective_dataset_ids(ctx: IntegrationContext) -> set:
    """Intersection of IntegrationSystem.allowed_datasets and User's visible departments.

    Even superusers are strictly bounded by IntegrationSystem.allowed_datasets (A06).
    """
    validate_system_active(ctx.system)
    validate_user_active(ctx.user)
    sys_ds_ids = set(ctx.system.allowed_datasets.values_list('id', flat=True))
    if not sys_ds_ids:
        return set()
    if ctx.user.is_superuser:
        return sys_ds_ids
    user_depts = set(dept_ids(ctx.user))
    if not user_depts:
        return set()
    user_ds_ids = set(
        Dataset.objects.filter(id__in=sys_ds_ids, item__department_id__in=user_depts).values_list('id', flat=True)
    )
    return sys_ds_ids & user_ds_ids


def effective_photo_queryset(ctx: IntegrationContext):
    """Authorized submitted-only Photo queryset for the current dual-layer context."""
    ds_ids = effective_dataset_ids(ctx)
    if not ds_ids:
        return Photo.objects.none()
    return Photo.objects.select_related('dataset__item', 'uploader').filter(
        status='submitted',
        dataset_id__in=ds_ids,
    )


def can_download_original(ctx: IntegrationContext, photo: Photo) -> bool:
    caps = normalize_capabilities(ctx.system.capabilities)
    if not caps.get('original_download', False):
        return False
    if ctx.scopes is not None and 'photos:original' not in ctx.scopes:
        return False
    # Spec 7.1: Do not leak hidden GPS/timestamp via raw original EXIF when disallowed in JSON
    af = normalize_allowed_fields(ctx.system.allowed_fields)
    if not af.get('location', True) or not af.get('captured', True):
        return False
    if photo.status != 'submitted' or photo.dataset_id not in effective_dataset_ids(ctx):
        return False
    return allowed(ctx.user, photo.dataset.item.department_id, 'download')


def can_report_import(ctx: IntegrationContext, photo: Photo) -> bool:
    if ctx.actor_type != 'user':
        return False
    caps = normalize_capabilities(ctx.system.capabilities)
    if not caps.get('report_import', False):
        return False
    if ctx.scopes is not None and 'reports:import' not in ctx.scopes:
        return False
    return photo.status == 'submitted' and photo.dataset_id in effective_dataset_ids(ctx)


def can_preview(ctx: IntegrationContext, photo: Photo) -> bool:
    caps = normalize_capabilities(ctx.system.capabilities)
    if not caps.get('thumbnail', False):
        return False
    if ctx.scopes is not None and 'photos:preview' not in ctx.scopes:
        return False
    return photo.status == 'submitted' and photo.dataset_id in effective_dataset_ids(ctx)


def authenticate_bearer_token(request, required_audience=None, channel='api') -> IntegrationContext:
    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth_header.startswith('Bearer '):
        raise IntegrationError('UNAUTHENTICATED', '缺少 Bearer 存取憑證', status=401)
    raw_token = auth_header[7:].strip()
    if not raw_token:
        raise IntegrationError('UNAUTHENTICATED', '存取憑證為空', status=401)
    return authenticate_raw_token(raw_token, required_audience=required_audience, channel=channel)


def authenticate_raw_token(raw_token: str, required_audience=None, channel='api') -> IntegrationContext:
    token = (
        IntegrationToken.objects.select_related('system', 'user')
        .filter(token_sha=hash_secret(raw_token))
        .first()
    )
    if not token:
        raise IntegrationError('UNAUTHENTICATED', '存取憑證無效', status=401)
    if token.revoked:
        raise IntegrationError('UNAUTHENTICATED', '存取憑證已撤銷', status=401)
    if token.expires_at <= timezone.now():
        raise IntegrationError('TOKEN_EXPIRED', '存取憑證已過期，請重新取得', status=401)
    if required_audience:
        allowed_audiences = (required_audience,) if isinstance(required_audience, str) else tuple(required_audience)
        if token.audience not in allowed_audiences:
            raise IntegrationError(
                'PERMISSION_DENIED',
                '此憑證的接收對象不適用於目前介面',
                status=403,
                details={'expected_audience': list(allowed_audiences)},
            )
    validate_system_active(token.system)
    validate_user_active(token.user)
    if token.actor_type == 'service' and not token.system.allow_service_token:
        raise IntegrationError('PERMISSION_DENIED', '此串接系統未啟用服務身分憑證', status=403)
    check_rate_limit(token.system)
    return IntegrationContext(
        system=token.system,
        user=token.user,
        actor_type=token.actor_type,
        channel=channel,
        scopes=tuple(token.scopes or ()),
        audience=token.audience,
        token_id=str(token.id),
    )


def authenticate_picker_session(raw_token: str) -> tuple:
    if not raw_token:
        raise IntegrationError('UNAUTHENTICATED', '缺少挑選工作階段憑證', status=401)
    session = (
        PickerSession.objects.select_related('system', 'user')
        .filter(token_sha=hash_secret(raw_token))
        .first()
    )
    if not session or session.revoked:
        raise IntegrationError('UNAUTHENTICATED', '挑選工作階段無效或已撤銷', status=401)
    if session.expires_at <= timezone.now():
        raise IntegrationError('TOKEN_EXPIRED', '挑選工作階段已過期，請從業務系統重新開啟', status=401)
    validate_system_active(session.system)
    validate_user_active(session.user)
    check_rate_limit(session.system)
    ctx = IntegrationContext(
        system=session.system,
        user=session.user,
        actor_type='user',
        channel='picker',
        scopes=('datasets:read', 'photos:search', 'photos:preview', 'reports:import', 'picker:use'),
        audience='picker',
        picker_session_id=str(session.id),
    )
    require_capability(ctx, 'picker', 'picker:use')
    return ctx, session


def issue_image_ticket(
    ctx: IntegrationContext,
    photo_id: int,
    variant: str = 'preview',
    purpose: str = 'preview',
    import_item_id: str = '',
    ttl_seconds: int = DEFAULT_TICKET_TTL,
) -> dict:
    ttl = max(30, min(MAX_TICKET_TTL, int(ttl_seconds)))
    exp = int(time.time()) + ttl
    payload = {
        'sid': str(ctx.system.id),
        'uid': int(ctx.user.id),
        'at': ctx.actor_type,
        'pid': int(photo_id),
        'v': str(variant),
        'p': str(purpose),
        'iid': str(import_item_id or ''),
        'sc': list(ctx.scopes) if ctx.scopes is not None else None,
        'exp': exp,
    }
    ticket = signing.dumps(payload, salt=IMAGE_TICKET_SALT)
    return {
        'ticket': ticket,
        'url': f'/integration/v1/assets/{ticket}',
        'variant': variant,
        'purpose': purpose,
        'expires_at': (timezone.now() + timedelta(seconds=ttl)).isoformat(),
        'ttl_seconds': ttl,
    }


def verify_image_ticket(ticket: str, expected_system_id: str = None, expected_photo_id: int = None) -> tuple:
    try:
        payload = signing.loads(ticket, salt=IMAGE_TICKET_SALT, max_age=MAX_TICKET_TTL + 60)
    except signing.SignatureExpired as exc:
        raise IntegrationError('TOKEN_EXPIRED', '圖片取用憑證已過期', status=401) from exc
    except signing.BadSignature as exc:
        raise IntegrationError('UNAUTHENTICATED', '圖片取用憑證無效', status=401) from exc

    if int(payload.get('exp', 0)) < int(time.time()):
        raise IntegrationError('TOKEN_EXPIRED', '圖片取用憑證已過期', status=401)
    if expected_system_id and str(expected_system_id) != str(payload.get('sid')):
        raise IntegrationError('PERMISSION_DENIED', '圖片憑證不可跨系統使用', status=403)
    if expected_photo_id is not None and int(expected_photo_id) != int(payload.get('pid', -1)):
        raise IntegrationError('PERMISSION_DENIED', '圖片憑證不可轉用於其他照片', status=403)

    system = IntegrationSystem.objects.filter(id=payload.get('sid')).first()
    if not system:
        raise IntegrationError('PERMISSION_DENIED', '串接系統不存在或已停用', status=403)
    validate_system_active(system)

    from django.contrib.auth.models import User

    user = User.objects.filter(id=payload.get('uid')).first()
    validate_user_active(user)

    ctx = IntegrationContext(
        system=system,
        user=user,
        actor_type=payload.get('at', 'user'),
        channel='api',
        scopes=tuple(payload['sc']) if payload.get('sc') is not None else None,
        audience='asset-ticket',
    )
    return ctx, payload


def _sanitize_audit_detail(detail):
    """Strip any secrets, tokens, or tickets from audit details."""
    if not isinstance(detail, dict):
        return {}
    forbidden_keys = {'token', 'access_token', 'client_secret', 'secret', 'ticket', 'password', 'authorization'}
    clean = {}
    for k, v in detail.items():
        if str(k).lower() in forbidden_keys:
            clean[k] = '[REDACTED]'
        elif isinstance(v, str) and len(v) > 300:
            clean[k] = v[:300] + '…'
        else:
            clean[k] = v
    return clean


def integration_audit(
    ctx,
    action: str,
    outcome: str = 'success',
    error_code: str = '',
    search_id: str = '',
    dataset_ids=None,
    photo_ids=None,
    case_id: str = '',
    report_id: str = '',
    import_job_id: str = '',
    detail=None,
    channel: str = None,
):
    system = ctx.system if isinstance(ctx, IntegrationContext) else None
    user = ctx.user if isinstance(ctx, IntegrationContext) else None
    actor_type = ctx.actor_type if isinstance(ctx, IntegrationContext) else 'user'
    chan = channel or (ctx.channel if isinstance(ctx, IntegrationContext) else 'api')
    IntegrationAudit.objects.create(
        system=system,
        user=user,
        actor_type=actor_type,
        channel=chan,
        action=action,
        search_id=str(search_id or ''),
        dataset_ids=list(dataset_ids or []),
        photo_ids=[int(x) for x in (photo_ids or [])][:200],
        case_id=str(case_id or ''),
        report_id=str(report_id or ''),
        import_job_id=str(import_job_id or ''),
        outcome=outcome,
        error_code=str(error_code or ''),
        detail=_sanitize_audit_detail(detail or {}),
    )
