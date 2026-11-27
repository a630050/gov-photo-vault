"""HTTP endpoints for Integration API v1, HTTP MCP, OAuth/OIDC, Reusable Photo Picker, and Admin."""
import base64
import hashlib
import json
from datetime import timedelta
from urllib.parse import quote, urlparse
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from .helpers import Problem, audit
from .integration_auth import (
    INTEGRATION_SCOPES,
    SERVICE_ALLOWED_SCOPES,
    IntegrationContext,
    IntegrationError,
    authenticate_bearer_token,
    authenticate_picker_session,
    generate_opaque_token,
    hash_secret,
    integration_audit,
    normalize_allowed_fields,
    normalize_capabilities,
    require_capability,
    validate_system_active,
    validate_user_active,
)
from .integration_service import (
    service_capabilities,
    service_create_report_import,
    service_get_report_import,
    service_issue_preview_ticket,
    service_list_datasets,
    service_resolve_photos,
    service_retry_report_import,
    service_search_photos,
    service_serve_asset_by_ticket,
)
from .integration_maintenance import (
    DEFAULT_AUDIT_DETAIL_DAYS,
    DEFAULT_REPORT_RETENTION_DAYS,
    DEFAULT_TOKEN_RETENTION_DAYS,
    integration_storage_stats,
    run_integration_cleanup,
)
from .integration_openapi import build_integration_openapi_spec
from .mcp_server import handle_mcp_request
from .models import (
    Dataset,
    ExternalIdentityBinding,
    IntegrationAudit,
    IntegrationAuthCode,
    IntegrationSystem,
    IntegrationSystemEvent,
    IntegrationToken,
    PickerSession,
    ReportImportJob,
)


def _json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError('INVALID_CRITERIA', '請求內容須為合法 JSON', status=400) from exc


def _error_response(exc: IntegrationError, request=None, mcp_www_auth=False):
    resp = JsonResponse(exc.to_dict(), status=exc.status)
    resp['Cache-Control'] = 'no-store'
    if mcp_www_auth and exc.status == 401 and request is not None:
        base = request.build_absolute_uri('/').rstrip('/')
        resp['WWW-Authenticate'] = (
            f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
        )
    return resp


def _normalize_origin(origin: str) -> str:
    raw = str(origin or '').strip()
    if not raw or any(ch.isspace() or ord(ch) < 32 for ch in raw) or ';' in raw or ',' in raw:
        return ''
    parsed = urlparse(raw)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or not parsed.hostname:
        return ''
    if parsed.username is not None or parsed.password is not None:
        return ''
    if parsed.path not in ('', '/') or parsed.params or parsed.query or parsed.fragment:
        return ''
    try:
        _ = parsed.port
    except ValueError:
        return ''
    if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost'):
        return ''
    return f'{parsed.scheme}://{parsed.netloc}'.lower()


def _origin_allowed(origin: str, allowed_origins: list) -> bool:
    normalized = _normalize_origin(origin)
    if not normalized:
        return False
    allowed_set = {
        norm for o in (allowed_origins or []) if (norm := _normalize_origin(o))
    }
    return normalized in allowed_set


def _system_snapshot(sys_obj: IntegrationSystem) -> dict:
    return {
        'code': sys_obj.code,
        'name': sys_obj.name,
        'owner_unit': sys_obj.owner_unit,
        'contact': sys_obj.contact,
        'allowed_datasets': sorted(sys_obj.allowed_datasets.values_list('id', flat=True)),
        'capabilities': normalize_capabilities(sys_obj.capabilities),
        'allowed_fields': normalize_allowed_fields(sys_obj.allowed_fields),
        'active': sys_obj.active,
        'revoked': sys_obj.revoked,
        'valid_from': sys_obj.valid_from.isoformat() if sys_obj.valid_from else None,
        'valid_until': sys_obj.valid_until.isoformat() if sys_obj.valid_until else None,
        'max_results_per_page': sys_obj.max_results_per_page,
        'rate_limit_per_minute': sys_obj.rate_limit_per_minute,
        'max_import_count': sys_obj.max_import_count,
        'max_preview_per_call': sys_obj.max_preview_per_call,
        'allowed_origins': list(sys_obj.allowed_origins or []),
        'allow_service_token': sys_obj.allow_service_token,
        'service_user_id': sys_obj.service_user_id,
    }


# ---------------------------------------------------------------------------
# OAuth / OIDC & MCP Discovery Metadata
# ---------------------------------------------------------------------------

def oauth_protected_resource_metadata(request):
    base = request.build_absolute_uri('/').rstrip('/')
    return JsonResponse({
        'resource': f'{base}/mcp',
        'authorization_servers': [base],
        'scopes_supported': list(INTEGRATION_SCOPES),
        'bearer_methods_supported': ['header'],
    })


def oauth_authorization_server_metadata(request):
    base = request.build_absolute_uri('/').rstrip('/')
    return JsonResponse({
        'issuer': base,
        'authorization_endpoint': f'{base}/integration/oauth/authorize',
        'token_endpoint': f'{base}/integration/oauth/token',
        'revocation_endpoint': f'{base}/integration/oauth/revoke',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'client_credentials', 'identity_exchange'],
        'code_challenge_methods_supported': ['S256'],
        'scopes_supported': list(INTEGRATION_SCOPES),
    })


def oauth_authorize(request):
    """Issue a short-lived authorization code for the currently logged-in archive user."""
    try:
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 POST', status=405)
        if not request.user.is_authenticated or not request.user.is_active:
            raise IntegrationError('UNAUTHENTICATED', '請先登入照片歸檔系統', status=401)
        data = _json_body(request)
        client_id = str(data.get('client_id') or '').strip()
        redirect_uri = str(data.get('redirect_uri') or '').strip()
        audience = str(data.get('audience') or 'integration-api').strip()
        scopes = data.get('scopes') or list(INTEGRATION_SCOPES)
        code_challenge = str(data.get('code_challenge') or '').strip()
        code_challenge_method = str(data.get('code_challenge_method') or '').strip()

        sys_obj = IntegrationSystem.objects.filter(code=client_id).first()
        if not sys_obj:
            raise IntegrationError('PERMISSION_DENIED', '未知的串接系統', status=403)
        validate_system_active(sys_obj)

        parsed = urlparse(redirect_uri)
        origin = f'{parsed.scheme}://{parsed.netloc}' if parsed.scheme and parsed.netloc else ''
        if not _origin_allowed(origin, sys_obj.allowed_origins):
            raise IntegrationError('PERMISSION_DENIED', 'redirect_uri 不在允許的來源網域清單內', status=403)

        if audience not in ('integration-api', 'mcp'):
            raise IntegrationError('INVALID_CRITERIA', 'audience 不合法', status=422)
        clean_scopes = [s for s in scopes if s in INTEGRATION_SCOPES]
        if not clean_scopes:
            raise IntegrationError('INVALID_CRITERIA', '至少須指定一個有效 scope', status=422)

        raw_code = generate_opaque_token('iac')
        IntegrationAuthCode.objects.create(
            code_sha=hash_secret(raw_code),
            system=sys_obj,
            user=request.user,
            redirect_uri=redirect_uri,
            audience=audience,
            scopes=clean_scopes,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        ctx = IntegrationContext(system=sys_obj, user=request.user, channel='api')
        integration_audit(ctx, 'oauth_authorize', detail={'audience': audience, 'scopes': clean_scopes})
        return JsonResponse({'code': raw_code, 'expires_in': 300})
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def oauth_token(request):
    """Exchange authorization_code (+PKCE), verified external identity binding, or service client_credentials."""
    try:
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 POST', status=405)
        data = _json_body(request)
        grant_type = str(data.get('grant_type') or '').strip()
        client_id = str(data.get('client_id') or '').strip()
        client_secret = str(data.get('client_secret') or '').strip()

        sys_obj = IntegrationSystem.objects.filter(code=client_id).first()
        if not sys_obj or not sys_obj.client_secret_sha or hash_secret(client_secret) != sys_obj.client_secret_sha:
            raise IntegrationError('UNAUTHENTICATED', '串接系統憑證驗證失敗', status=401)
        validate_system_active(sys_obj)

        try:
            ttl_seconds = min(3600, max(60, int(data.get('expires_in', 1800))))
        except (TypeError, ValueError) as exc:
            raise IntegrationError('INVALID_CRITERIA', 'expires_in 格式錯誤', status=422) from exc

        if grant_type == 'authorization_code':
            raw_code = str(data.get('code') or '').strip()
            redirect_uri = str(data.get('redirect_uri') or '').strip()
            code_verifier = str(data.get('code_verifier') or '').strip()
            with transaction.atomic():
                auth_code = (
                    IntegrationAuthCode.objects.select_for_update()
                    .select_related('user')
                    .filter(code_sha=hash_secret(raw_code), system=sys_obj)
                    .first()
                )
                if not auth_code or auth_code.used or auth_code.expires_at <= timezone.now():
                    raise IntegrationError('UNAUTHENTICATED', '授權碼無效、已使用或已過期', status=401)
                if auth_code.redirect_uri != redirect_uri:
                    raise IntegrationError('PERMISSION_DENIED', 'redirect_uri 不符', status=403)
                if auth_code.code_challenge:
                    if auth_code.code_challenge_method != 'S256':
                        raise IntegrationError('INVALID_CRITERIA', '僅支援 PKCE S256', status=422)
                    try:
                        verifier_bytes = code_verifier.encode('ascii')
                    except UnicodeEncodeError as exc:
                        raise IntegrationError('UNAUTHENTICATED', 'PKCE code_verifier 格式不合法', status=401) from exc
                    digest = base64.urlsafe_b64encode(
                        hashlib.sha256(verifier_bytes).digest()
                    ).rstrip(b'=').decode('ascii')
                    if digest != auth_code.code_challenge:
                        raise IntegrationError('UNAUTHENTICATED', 'PKCE 驗證失敗', status=401)
                validate_user_active(auth_code.user)
                auth_code.used = True
                auth_code.save(update_fields=['used'])
                user = auth_code.user
                audience = auth_code.audience
                scopes = list(auth_code.scopes)
                actor_type = 'user'

        elif grant_type == 'identity_exchange':
            issuer = str(data.get('issuer') or '').strip()
            subject = str(data.get('subject') or '').strip()
            audience = str(data.get('audience') or 'integration-api').strip()
            if audience not in ('integration-api', 'mcp'):
                raise IntegrationError('INVALID_CRITERIA', 'audience 不合法', status=422)
            req_scopes = data.get('scopes')
            if req_scopes is None:
                req_scopes = ['datasets:read', 'photos:search', 'photos:preview', 'reports:import']
            if not isinstance(req_scopes, list):
                raise IntegrationError('INVALID_CRITERIA', 'scopes 須為陣列', status=422)
            if not issuer or not subject:
                raise IntegrationError('INVALID_CRITERIA', '須提供經驗證的 issuer 與 subject', status=422)
            binding = (
                ExternalIdentityBinding.objects.select_related('user')
                .filter(system=sys_obj, issuer=issuer, subject=subject, active=True)
                .first()
            )
            if not binding:
                raise IntegrationError('UNAUTHENTICATED', '找不到經驗證的人員身分對應，無法核發憑證', status=401)
            validate_user_active(binding.user)
            user = binding.user
            scopes = [s for s in req_scopes if s in INTEGRATION_SCOPES]
            if not scopes:
                raise IntegrationError('INVALID_CRITERIA', '至少須指定一個有效 scope', status=422)
            actor_type = 'user'

        elif grant_type == 'client_credentials':
            if not sys_obj.allow_service_token or not sys_obj.service_user:
                raise IntegrationError('PERMISSION_DENIED', '此串接系統未啟用獨立服務身分憑證', status=403)
            validate_user_active(sys_obj.service_user)
            user = sys_obj.service_user
            audience = str(data.get('audience') or 'integration-api').strip()
            if audience != 'integration-api':
                raise IntegrationError('PERMISSION_DENIED', '服務身分憑證僅限 integration-api，不可用於互動式 MCP', status=403)
            req_scopes = data.get('scopes')
            if req_scopes is None:
                req_scopes = ['datasets:read', 'photos:search']
            if not isinstance(req_scopes, list):
                raise IntegrationError('INVALID_CRITERIA', 'scopes 須為陣列', status=422)
            forbidden = [s for s in req_scopes if s in INTEGRATION_SCOPES and s not in SERVICE_ALLOWED_SCOPES]
            if forbidden:
                raise IntegrationError(
                    'PERMISSION_DENIED',
                    '服務身分憑證不可申請互動式權限範圍（如 reports:import、picker:use、mcp:tools）',
                    status=403,
                    details={'forbidden_scopes': forbidden},
                )
            scopes = [s for s in req_scopes if s in SERVICE_ALLOWED_SCOPES]
            if not scopes:
                raise IntegrationError('INVALID_CRITERIA', '至少須指定一個有效的服務身分 scope', status=422)
            actor_type = 'service'
        else:
            raise IntegrationError('INVALID_CRITERIA', f'不支援的 grant_type：{grant_type}', status=422)

        raw_token = generate_opaque_token('itk')
        expires_at = timezone.now() + timedelta(seconds=ttl_seconds)
        tok = IntegrationToken.objects.create(
            system=sys_obj,
            user=user,
            actor_type=actor_type,
            token_sha=hash_secret(raw_token),
            audience=audience,
            scopes=scopes,
            expires_at=expires_at,
        )
        ctx = IntegrationContext(system=sys_obj, user=user, actor_type=actor_type, channel='api')
        integration_audit(
            ctx,
            'oauth_token_issued',
            detail={'grant_type': grant_type, 'audience': audience, 'scopes': scopes, 'token_id': str(tok.id)},
        )
        resp = JsonResponse({
            'access_token': raw_token,
            'token_type': 'Bearer',
            'expires_in': ttl_seconds,
            'expires_at': expires_at.isoformat(),
            'audience': audience,
            'scope': ' '.join(scopes),
            'actor_type': actor_type,
        })
        resp['Cache-Control'] = 'no-store'
        return resp
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def oauth_revoke(request):
    try:
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 POST', status=405)
        data = _json_body(request)
        raw_token = str(data.get('token') or '').strip()
        if not raw_token:
            raise IntegrationError('INVALID_CRITERIA', '請提供要撤銷的 token', status=422)
        tok = IntegrationToken.objects.select_related('system', 'user').filter(token_sha=hash_secret(raw_token)).first()
        if tok:
            tok.revoked = True
            tok.save(update_fields=['revoked'])
            ctx = IntegrationContext(system=tok.system, user=tok.user, actor_type=tok.actor_type, channel='api')
            integration_audit(ctx, 'oauth_token_revoked', detail={'token_id': str(tok.id)})
        return JsonResponse({'ok': True})
    except IntegrationError as exc:
        return _error_response(exc)


# ---------------------------------------------------------------------------
# Integration API v1
# ---------------------------------------------------------------------------

@csrf_exempt
def integration_capabilities_view(request):
    try:
        if request.method != 'GET':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 GET', status=405)
        ctx = authenticate_bearer_token(request, required_audience=('integration-api', 'mcp'), channel='api')
        return JsonResponse(service_capabilities(ctx))
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def integration_datasets_view(request):
    try:
        if request.method != 'GET':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 GET', status=405)
        ctx = authenticate_bearer_token(request, required_audience=('integration-api', 'mcp'), channel='api')
        return JsonResponse(service_list_datasets(ctx))
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def integration_search_view(request):
    try:
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 POST', status=405)
        ctx = authenticate_bearer_token(request, required_audience=('integration-api', 'mcp'), channel='api')
        data = _json_body(request)
        return JsonResponse(service_search_photos(ctx, data))
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def integration_resolve_view(request):
    try:
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 POST', status=405)
        ctx = authenticate_bearer_token(request, required_audience=('integration-api', 'mcp'), channel='api')
        data = _json_body(request)
        return JsonResponse(
            service_resolve_photos(ctx, data.get('photo_ids'), expected_versions=data.get('expected_versions'))
        )
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def integration_preview_ticket_view(request, photo_id):
    try:
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 POST', status=405)
        ctx = authenticate_bearer_token(request, required_audience=('integration-api', 'mcp'), channel='api')
        return JsonResponse(service_issue_preview_ticket(ctx, int(photo_id)))
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def integration_report_imports_view(request):
    try:
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 POST', status=405)
        ctx = authenticate_bearer_token(request, required_audience='integration-api', channel='api')
        data = _json_body(request)
        result = service_create_report_import(ctx, data)
        status_code = 200 if result.get('idempotent_replay') else 201
        return JsonResponse(result, status=status_code)
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def integration_report_import_detail_view(request, job_id):
    try:
        if request.method != 'GET':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 GET', status=405)
        ctx = authenticate_bearer_token(request, required_audience='integration-api', channel='api')
        return JsonResponse(service_get_report_import(ctx, job_id))
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def integration_report_import_retry_view(request, job_id):
    try:
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 POST', status=405)
        ctx = authenticate_bearer_token(request, required_audience='integration-api', channel='api')
        return JsonResponse(service_retry_report_import(ctx, job_id))
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def integration_asset_view(request, ticket):
    try:
        if request.method != 'GET':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 GET', status=405)
        expected_system_id = None
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            caller_ctx = authenticate_bearer_token(request, channel='api')
            expected_system_id = str(caller_ctx.system.id)
        expected_photo_id = None
        if request.GET.get('photo_id'):
            try:
                expected_photo_id = int(request.GET['photo_id'])
            except ValueError as exc:
                raise IntegrationError('INVALID_CRITERIA', 'photo_id 格式錯誤', status=422) from exc
        raw, content_type, filename, sha, size = service_serve_asset_by_ticket(
            ticket,
            expected_system_id=expected_system_id,
            expected_photo_id=expected_photo_id,
        )
        resp = HttpResponse(raw, content_type=content_type)
        resp['Cache-Control'] = 'private, no-store'
        resp['Content-Length'] = str(size)
        resp['X-Content-SHA256'] = sha
        safe_ascii = ''.join(ch if 32 <= ord(ch) < 127 and ch not in '"\\;\r\n' else '_' for ch in str(filename or 'photo.jpg'))
        resp['Content-Disposition'] = f"inline; filename=\"{safe_ascii}\"; filename*=UTF-8''{quote(str(filename or 'photo.jpg'))}"
        return resp
    except IntegrationError as exc:
        return _error_response(exc)


@csrf_exempt
def integration_picker_session_view(request):
    """Create a short-lived picker session for iframe/popup embedding."""
    try:
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', '僅支援 POST', status=405)
        ctx = authenticate_bearer_token(request, required_audience='integration-api', channel='api')
        if ctx.actor_type != 'user':
            raise IntegrationError('PERMISSION_DENIED', '挑選工作階段須由互動式操作人員啟動，不可使用服務身分憑證', status=403)
        require_capability(ctx, 'picker', 'picker:use')
        data = _json_body(request)
        origin = str(data.get('origin') or '').strip()
        if not _origin_allowed(origin, ctx.system.allowed_origins):
            raise IntegrationError('PERMISSION_DENIED', '指定的來源網域不在串接系統允許清單內', status=403)
        callback_url = str(data.get('callback_url') or '').strip()
        if callback_url:
            cb_parsed = urlparse(callback_url)
            cb_origin = f'{cb_parsed.scheme}://{cb_parsed.netloc}'
            if not _origin_allowed(cb_origin, ctx.system.allowed_origins):
                raise IntegrationError('PERMISSION_DENIED', '回呼網址不在串接系統允許來源清單內', status=403)

        try:
            req_max = int(data.get('max_select', ctx.system.max_import_count))
            ttl = max(60, min(1800, int(data.get('ttl_seconds', 900))))
        except (TypeError, ValueError) as exc:
            raise IntegrationError('INVALID_CRITERIA', 'max_select 或 ttl_seconds 格式錯誤', status=422) from exc
        max_select = max(1, min(int(ctx.system.max_import_count or 50), req_max))
        raw_token = generate_opaque_token('pks')
        expires_at = timezone.now() + timedelta(seconds=ttl)
        defaults = data.get('defaults') if isinstance(data.get('defaults'), dict) else {}
        session = PickerSession.objects.create(
            token_sha=hash_secret(raw_token),
            system=ctx.system,
            user=ctx.user,
            case_id=str(data.get('case_id') or '').strip()[:120],
            report_id=str(data.get('report_id') or '').strip()[:120],
            origin=_normalize_origin(origin),
            callback_url=callback_url[:500],
            defaults_payload=defaults,
            max_select=max_select,
            expires_at=expires_at,
        )
        integration_audit(
            ctx,
            'picker_session_created',
            case_id=session.case_id,
            report_id=session.report_id,
            detail={'session_id': str(session.id), 'origin': session.origin, 'max_select': max_select},
        )
        return JsonResponse({
            'session_id': str(session.id),
            'picker_token': raw_token,
            'picker_url': f'/picker?session={raw_token}',
            'origin': session.origin,
            'max_select': max_select,
            'expires_at': expires_at.isoformat(),
        }, status=201)
    except IntegrationError as exc:
        return _error_response(exc)


# ---------------------------------------------------------------------------
# HTTP MCP Endpoint (/mcp)
# ---------------------------------------------------------------------------

@csrf_exempt
def mcp_http_view(request):
    try:
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', 'MCP HTTP 端點僅支援 POST', status=405)
        ctx = authenticate_bearer_token(request, required_audience=('mcp', 'integration-api'), channel='mcp')
        payload = _json_body(request)
        response = handle_mcp_request(ctx, payload)
        if response is None:
            return HttpResponse(status=204)
        resp = JsonResponse(response)
        resp['Cache-Control'] = 'no-store'
        return resp
    except IntegrationError as exc:
        return _error_response(exc, request=request, mcp_www_auth=True)


# ---------------------------------------------------------------------------
# Reusable Photo Picker UI & Session API
# ---------------------------------------------------------------------------

@xframe_options_exempt
def picker_page(request):
    resp = render(request, 'picker.html')
    resp['Cache-Control'] = 'no-store'
    resp['Referrer-Policy'] = 'no-referrer'
    frame_ancestors = ["'none'"]
    raw_session = (request.GET.get('session') or '').strip()
    if raw_session:
        sess = PickerSession.objects.select_related('system', 'user').filter(
            token_sha=hash_secret(raw_session),
            revoked=False,
            expires_at__gt=timezone.now(),
        ).first()
        if sess:
            try:
                validate_system_active(sess.system)
                validate_user_active(sess.user)
                clean_origin = _normalize_origin(sess.origin)
                if clean_origin and _origin_allowed(clean_origin, sess.system.allowed_origins):
                    frame_ancestors = ["'self'", clean_origin]
            except IntegrationError:
                pass
    resp['Content-Security-Policy'] = f"frame-ancestors {' '.join(frame_ancestors)}"
    return resp


def _picker_ctx(request):
    raw_token = request.META.get('HTTP_X_PICKER_SESSION', '').strip()
    return authenticate_picker_session(raw_token)


@csrf_exempt
def picker_api_view(request, action):
    try:
        ctx, session = _picker_ctx(request)
        if action == 'bootstrap' and request.method == 'GET':
            caps = service_capabilities(ctx)
            ds = service_list_datasets(ctx)
            return JsonResponse({
                'session_id': str(session.id),
                'case_id': session.case_id,
                'report_id': session.report_id,
                'origin': session.origin,
                'callback_url': session.callback_url,
                'max_select': session.max_select,
                'defaults': session.defaults_payload,
                'expires_at': session.expires_at.isoformat(),
                'capabilities': caps['capabilities'],
                'system': caps['system'],
                'actor': caps['actor'],
                'datasets': ds['datasets'],
            })
        if request.method != 'POST':
            raise IntegrationError('INVALID_CRITERIA', '不支援的方法', status=405)
        data = _json_body(request)
        if action == 'search':
            res = service_search_photos(ctx, data, include_preview_ticket=True)
            return JsonResponse(res)
        if action == 'resolve':
            res = service_resolve_photos(
                ctx,
                data.get('photo_ids'),
                expected_versions=data.get('expected_versions'),
                include_preview_ticket=True,
            )
            return JsonResponse(res)
        if action == 'preview-ticket':
            try:
                pid = int(data['photo_id'])
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrationError('INVALID_CRITERIA', 'photo_id 格式錯誤', status=422) from exc
            return JsonResponse(service_issue_preview_ticket(ctx, pid))
        if action == 'import':
            payload = {
                'idempotency_key': data.get('idempotency_key'),
                'case_id': session.case_id or data.get('case_id', ''),
                'report_id': session.report_id or data.get('report_id', ''),
                'items': data.get('items'),
            }
            return JsonResponse(service_create_report_import(ctx, payload, max_count_override=session.max_select))
        raise IntegrationError('INVALID_CRITERIA', '未知挑選器操作', status=404)
    except IntegrationError as exc:
        return _error_response(exc)


# ---------------------------------------------------------------------------
# Disaster-Response End-to-End Demo Workbench (/integration-demo)
# ---------------------------------------------------------------------------

@ensure_csrf_cookie
def integration_demo_page(request):
    from django.conf import settings
    return render(request, 'integration_demo.html', {'csrf_cookie_name': settings.CSRF_COOKIE_NAME})


def integration_openapi_view(request):
    if request.method != 'GET':
        return _error_response(IntegrationError('INVALID_CRITERIA', '僅支援 GET', status=405))
    base = request.build_absolute_uri('/').rstrip('/')
    resp = JsonResponse(build_integration_openapi_spec(base))
    resp['Cache-Control'] = 'no-store'
    return resp


def integration_docs_view(request):
    if request.method not in ('GET', 'HEAD'):
        return _error_response(IntegrationError('INVALID_CRITERIA', '僅支援 GET', status=405))
    base = request.build_absolute_uri('/').rstrip('/')
    return render(request, 'integration_docs.html', {'base_url': base})


# ---------------------------------------------------------------------------
# Admin Management API for Integration Systems (called via /api/integration_admin/*)
# ---------------------------------------------------------------------------

def handle_integration_admin_api(path, data, u):
    if not u.is_superuser:
        raise Problem('僅系統管理者可管理串接系統授權', 403)

    if path == 'integration_admin/list':
        systems = []
        for s in IntegrationSystem.objects.prefetch_related('allowed_datasets').order_by('code'):
            systems.append({
                'id': str(s.id),
                **_system_snapshot(s),
                'created': s.created.isoformat(),
                'updated': s.updated.isoformat(),
            })
        bindings = [
            {
                'id': b.id,
                'system_id': str(b.system_id),
                'system_code': b.system.code,
                'issuer': b.issuer,
                'subject': b.subject,
                'user_id': b.user_id,
                'username': b.user.username,
                'active': b.active,
                'verified_at': b.verified_at.isoformat(),
            }
            for b in ExternalIdentityBinding.objects.select_related('system', 'user').order_by('-id')[:100]
        ]
        tokens = [
            {
                'id': str(t.id),
                'system_code': t.system.code,
                'username': t.user.username,
                'actor_type': t.actor_type,
                'audience': t.audience,
                'scopes': t.scopes,
                'expires_at': t.expires_at.isoformat(),
                'revoked': t.revoked,
                'created': t.created.isoformat(),
            }
            for t in IntegrationToken.objects.select_related('system', 'user').order_by('-created')[:100]
        ]
        events = [
            {
                'id': e.id,
                'system_code': e.system.code,
                'actor': e.actor.username if e.actor else '',
                'action': e.action,
                'reason': e.reason,
                'before': e.before,
                'after': e.after,
                'created': e.created.isoformat(),
            }
            for e in IntegrationSystemEvent.objects.select_related('system', 'actor').order_by('-id')[:100]
        ]
        audit_qs = IntegrationAudit.objects.select_related('system', 'user').order_by('-id')
        f_sys = str((data or {}).get('audit_system') or '').strip()
        f_act = str((data or {}).get('audit_action') or '').strip()
        f_out = str((data or {}).get('audit_outcome') or '').strip()
        f_case = str((data or {}).get('audit_case_id') or '').strip()
        if f_sys:
            audit_qs = audit_qs.filter(system__code=f_sys)
        if f_act:
            audit_qs = audit_qs.filter(action__icontains=f_act)
        if f_out:
            audit_qs = audit_qs.filter(outcome=f_out)
        if f_case:
            audit_qs = audit_qs.filter(case_id__icontains=f_case)
        audits = [
            {
                'id': a.id,
                'system_code': a.system.code if a.system else '',
                'username': a.user.username if a.user else '',
                'actor_type': a.actor_type,
                'channel': a.channel,
                'action': a.action,
                'search_id': a.search_id,
                'dataset_ids': a.dataset_ids,
                'photo_ids': a.photo_ids,
                'case_id': a.case_id,
                'report_id': a.report_id,
                'import_job_id': a.import_job_id,
                'outcome': a.outcome,
                'error_code': a.error_code,
                'detail': a.detail,
                'created': a.created.isoformat(),
            }
            for a in audit_qs[:100]
        ]
        imports = []
        for job in ReportImportJob.objects.select_related('system', 'user').prefetch_related('items').order_by('-created')[:50]:
            items_list = list(job.items.all())
            imports.append({
                'id': str(job.id),
                'system_code': job.system.code,
                'username': job.user.username,
                'case_id': job.case_id,
                'report_id': job.report_id,
                'state': job.state,
                'total': len(items_list),
                'ready': sum(1 for it in items_list if it.state == 'ready'),
                'expired': sum(1 for it in items_list if it.state == 'expired'),
                'failed': sum(1 for it in items_list if it.state not in ('ready', 'expired')),
                'confirmed_at': job.confirmed_at.isoformat(),
                'created': job.created.isoformat(),
            })
        dataset_fields = {}
        for d in Dataset.objects.select_related('item').all():
            fields_list = []
            if d.item_id and isinstance(d.item.fields, list):
                for f in d.item.fields:
                    fid = str(f.get('key') or f.get('id') or '').strip()
                    if fid:
                        fields_list.append({
                            'id': fid,
                            'label': str(f.get('label') or fid),
                            'type': str(f.get('type') or 'text'),
                        })
            dataset_fields[str(d.id)] = fields_list
        return JsonResponse({
            'systems': systems,
            'bindings': bindings,
            'tokens': tokens,
            'events': events,
            'audits': audits,
            'imports': imports,
            'dataset_fields': dataset_fields,
            'stats': integration_storage_stats(),
        })

    if path == 'integration_admin/save_system':
        reason = str(data.get('reason') or '').strip()
        if not reason:
            raise Problem('請填寫異動理由', 422)
        code = str(data.get('code') or '').strip()
        name = str(data.get('name') or '').strip()
        if not code or len(code) > 60 or not name or len(name) > 120:
            raise Problem('請填寫合法的系統識別碼與名稱', 422)

        def _parse_opt_dt(key):
            val = data.get(key)
            if not val:
                return None
            dt_v = parse_datetime(str(val))
            if not dt_v or timezone.is_naive(dt_v):
                raise Problem(f'{key} 須為含時區的 ISO 日期時間', 422)
            return dt_v

        valid_from = _parse_opt_dt('valid_from')
        valid_until = _parse_opt_dt('valid_until')
        if valid_from and valid_until and valid_from >= valid_until:
            raise Problem('啟用時間須早於到期時間', 422)

        sys_id = data.get('id')
        new_secret = None
        if sys_id:
            try:
                s = IntegrationSystem.objects.select_for_update().get(id=sys_id)
            except (IntegrationSystem.DoesNotExist, ValidationError, ValueError) as exc:
                raise Problem('找不到指定的串接系統', 404) from exc
            before = _system_snapshot(s)
            action = 'update'
        else:
            s = IntegrationSystem(code=code)
            before = {}
            action = 'create'
            new_secret = generate_opaque_token('ics')
            s.client_secret_sha = hash_secret(new_secret)

        try:
            max_results = max(1, min(200, int(data.get('max_results_per_page', 50))))
            rate_limit = max(1, min(1000, int(data.get('rate_limit_per_minute', 120))))
            max_import = max(1, min(200, int(data.get('max_import_count', 50))))
            max_preview = max(1, min(30, int(data.get('max_preview_per_call', 12))))
            ds_ids = [int(x) for x in (data.get('allowed_datasets') or [])]
        except (TypeError, ValueError) as exc:
            raise Problem('上限或資料集識別碼格式錯誤', 422) from exc

        s.code = code
        s.name = name
        s.owner_unit = str(data.get('owner_unit') or '').strip()[:120]
        s.contact = str(data.get('contact') or '').strip()[:200]
        s.capabilities = normalize_capabilities(data.get('capabilities'))
        s.allowed_fields = normalize_allowed_fields(data.get('allowed_fields'))
        s.active = bool(data.get('active', True))
        if 'revoked' in data:
            s.revoked = bool(data['revoked'])
        s.valid_from = valid_from
        s.valid_until = valid_until
        s.max_results_per_page = max_results
        s.rate_limit_per_minute = rate_limit
        s.max_import_count = max_import
        s.max_preview_per_call = max_preview
        s.allowed_origins = [str(o).strip().rstrip('/') for o in (data.get('allowed_origins') or []) if str(o).strip()]
        s.allow_service_token = bool(data.get('allow_service_token', False))
        svc_uid = data.get('service_user_id')
        if svc_uid:
            try:
                s.service_user = User.objects.get(id=int(svc_uid))
            except (User.DoesNotExist, TypeError, ValueError) as exc:
                raise Problem('指定的服務帳號不存在', 422) from exc
        else:
            s.service_user = None
        if data.get('rotate_secret') and sys_id:
            new_secret = generate_opaque_token('ics')
            s.client_secret_sha = hash_secret(new_secret)
            action = 'rotate_secret'
        try:
            s.save()
        except IntegrityError as exc:
            raise Problem(f'系統識別碼 {code} 已被使用', 409) from exc

        if s.revoked:
            IntegrationToken.objects.filter(system=s, revoked=False).update(revoked=True)
            PickerSession.objects.filter(system=s, revoked=False).update(revoked=True)

        valid_ds = list(Dataset.objects.filter(id__in=ds_ids))
        s.allowed_datasets.set(valid_ds)
        after = _system_snapshot(s)

        IntegrationSystemEvent.objects.create(
            system=s,
            actor=u,
            action=action,
            reason=reason,
            before=before,
            after=after,
        )
        audit(u, 'integration_system_' + action, s.code, {'reason': reason})
        res = {'ok': True, 'system': {'id': str(s.id), **after}}
        if new_secret:
            res['client_secret'] = new_secret
        return JsonResponse(res)

    if path == 'integration_admin/revoke_system':
        reason = str(data.get('reason') or '').strip()
        if not reason:
            raise Problem('請填寫撤銷理由', 422)
        try:
            s = IntegrationSystem.objects.select_for_update().get(id=data.get('id'))
        except (IntegrationSystem.DoesNotExist, ValidationError, ValueError) as exc:
            raise Problem('找不到指定的串接系統', 404) from exc
        before = _system_snapshot(s)
        s.revoked = True
        s.active = False
        s.save(update_fields=['revoked', 'active', 'updated'])
        IntegrationToken.objects.filter(system=s, revoked=False).update(revoked=True)
        PickerSession.objects.filter(system=s, revoked=False).update(revoked=True)
        after = _system_snapshot(s)
        IntegrationSystemEvent.objects.create(
            system=s, actor=u, action='revoke', reason=reason, before=before, after=after
        )
        audit(u, 'integration_system_revoke', s.code, {'reason': reason})
        return JsonResponse({'ok': True})

    if path == 'integration_admin/bind_identity':
        try:
            s = IntegrationSystem.objects.get(id=data.get('system_id'))
            target_user = User.objects.get(id=int(data.get('user_id')))
        except (IntegrationSystem.DoesNotExist, User.DoesNotExist, ValidationError, TypeError, ValueError) as exc:
            raise Problem('找不到指定的串接系統或人員帳號', 404) from exc
        issuer = str(data.get('issuer') or '').strip()
        subject = str(data.get('subject') or '').strip()
        if not issuer or not subject:
            raise Problem('請填寫發行者 (issuer) 與外部使用者識別 (subject)', 422)
        binding, _ = ExternalIdentityBinding.objects.update_or_create(
            system=s,
            issuer=issuer,
            subject=subject,
            defaults={
                'user': target_user,
                'active': bool(data.get('active', True)),
                'verified_by': u,
                'verified_at': timezone.now(),
            },
        )
        audit(u, 'integration_bind_identity', f'{s.code}:{subject}', {'user': target_user.username, 'active': binding.active})
        return JsonResponse({'ok': True, 'id': binding.id})

    if path == 'integration_admin/issue_token':
        try:
            s = IntegrationSystem.objects.get(id=data.get('system_id'))
            target_user = User.objects.get(id=int(data.get('user_id')))
        except (IntegrationSystem.DoesNotExist, User.DoesNotExist, ValidationError, TypeError, ValueError) as exc:
            raise Problem('找不到指定的串接系統或人員帳號', 404) from exc
        try:
            validate_system_active(s)
            validate_user_active(target_user)
        except IntegrationError as exc:
            raise Problem(exc.message, 422) from exc
        audience = str(data.get('audience') or 'integration-api').strip()
        if audience not in ('integration-api', 'mcp'):
            raise Problem('audience 僅支援 integration-api 或 mcp', 422)
        req_scopes = data.get('scopes')
        if req_scopes is None:
            req_scopes = list(INTEGRATION_SCOPES)
        if not isinstance(req_scopes, list):
            raise Problem('scopes 須為陣列', 422)
        scopes = [sc for sc in req_scopes if sc in INTEGRATION_SCOPES]
        if not scopes:
            raise Problem('至少須指定一個有效權限範圍 (scope)', 422)
        try:
            ttl_minutes = max(5, min(1440, int(data.get('ttl_minutes', 60))))
        except (TypeError, ValueError) as exc:
            raise Problem('ttl_minutes 格式錯誤', 422) from exc
        raw_token = generate_opaque_token('itk')
        expires_at = timezone.now() + timedelta(minutes=ttl_minutes)
        tok = IntegrationToken.objects.create(
            system=s,
            user=target_user,
            actor_type='user',
            token_sha=hash_secret(raw_token),
            audience=audience,
            scopes=scopes,
            expires_at=expires_at,
            created_by=u,
        )
        audit(u, 'integration_issue_token', str(tok.id), {'system': s.code, 'user': target_user.username, 'audience': audience})
        return JsonResponse({
            'ok': True,
            'token_id': str(tok.id),
            'access_token': raw_token,
            'expires_at': expires_at.isoformat(),
        })

    if path == 'integration_admin/revoke_token':
        try:
            tok = IntegrationToken.objects.get(id=data.get('id'))
        except (IntegrationToken.DoesNotExist, ValidationError, ValueError) as exc:
            raise Problem('找不到指定的存取憑證', 404) from exc
        tok.revoked = True
        tok.save(update_fields=['revoked'])
        audit(u, 'integration_revoke_token', str(tok.id), {'system': tok.system.code})
        return JsonResponse({'ok': True})

    if path == 'integration_admin/toggle_binding':
        try:
            binding = ExternalIdentityBinding.objects.select_related('system', 'user').get(id=int(data.get('id')))
        except (ExternalIdentityBinding.DoesNotExist, TypeError, ValueError) as exc:
            raise Problem('找不到指定的外部身分綁定', 404) from exc
        raw_active = data.get('active')
        new_active = (not binding.active) if raw_active is None else bool(raw_active)
        binding.active = new_active
        binding.verified_by = u
        binding.verified_at = timezone.now()
        binding.save(update_fields=['active', 'verified_by', 'verified_at'])
        audit(
            u,
            'integration_toggle_binding',
            f'{binding.system.code}:{binding.subject}',
            {'user': binding.user.username, 'active': binding.active},
        )
        return JsonResponse({'ok': True, 'id': binding.id, 'active': binding.active})

    if path == 'integration_admin/cleanup':
        raw_dry_run = data.get('dry_run')
        dry_run = True if raw_dry_run is None else bool(raw_dry_run)
        try:
            result = run_integration_cleanup(
                report_retention_days=data.get('report_retention_days', DEFAULT_REPORT_RETENTION_DAYS),
                token_retention_days=data.get('token_retention_days', DEFAULT_TOKEN_RETENTION_DAYS),
                audit_detail_days=data.get('audit_detail_days', DEFAULT_AUDIT_DETAIL_DAYS),
                dry_run=dry_run,
            )
        except ValueError as exc:
            raise Problem(str(exc), 422) from exc
        if not result['dry_run']:
            audit(u, 'integration_cleanup', 'maintenance', {
                'expired_report_files': result['expired_report_files'],
                'failed_report_files': result['failed_report_files'],
                'freed_bytes': result['freed_bytes'],
                'deleted_auth_codes': result['deleted_auth_codes'],
                'deleted_tokens': result['deleted_tokens'],
                'deleted_picker_sessions': result['deleted_picker_sessions'],
                'redacted_audits': result['redacted_audits'],
                'retention_policy': result['retention_policy'],
            })
        return JsonResponse({'ok': True, 'result': result, 'stats': integration_storage_stats()})

    return None
