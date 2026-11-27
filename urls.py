from django.urls import path
from django.http import FileResponse,Http404
from pathlib import Path
from core.views import home,api,asset,download_export
from core.people import reference_asset
from core.views_integration import (
    integration_asset_view,
    integration_capabilities_view,
    integration_datasets_view,
    integration_demo_page,
    integration_docs_view,
    integration_openapi_view,
    integration_picker_session_view,
    integration_preview_ticket_view,
    integration_report_import_detail_view,
    integration_report_import_retry_view,
    integration_report_imports_view,
    integration_resolve_view,
    integration_search_view,
    mcp_http_view,
    oauth_authorization_server_metadata,
    oauth_authorize,
    oauth_protected_resource_metadata,
    oauth_revoke,
    oauth_token,
    picker_api_view,
    picker_page,
)
from transfer_common.ops_django import live,ready,status
BASE=Path(__file__).parent
def static_file(request,name):
    if name not in ['app.js','compare.js','similarity.js','content-search.js','people.js','external.js','library.js','workflow.js','upload.js','admin.js','jobs.js','collections.js','picker.js','integration-demo.js','style.css','leaflet.js','leaflet.css']:raise Http404
    return FileResponse((BASE/'static'/name).open('rb'),content_type='text/javascript' if name.endswith('.js') else 'text/css')
urlpatterns=[
    path('health/live',live),
    path('health/ready',ready),
    path('ops/status',status),
    path('',home),
    path('picker',picker_page),
    path('integration-demo',integration_demo_page),
    path('.well-known/oauth-protected-resource',oauth_protected_resource_metadata),
    path('.well-known/oauth-authorization-server',oauth_authorization_server_metadata),
    path('integration/oauth/authorize',oauth_authorize),
    path('integration/oauth/token',oauth_token),
    path('integration/oauth/revoke',oauth_revoke),
    path('integration/v1/openapi.json',integration_openapi_view),
    path('integration/v1/docs',integration_docs_view),
    path('integration/v1/capabilities',integration_capabilities_view),
    path('integration/v1/datasets',integration_datasets_view),
    path('integration/v1/photos/search',integration_search_view),
    path('integration/v1/photos/resolve',integration_resolve_view),
    path('integration/v1/photos/<int:photo_id>/preview-ticket',integration_preview_ticket_view),
    path('integration/v1/report-imports',integration_report_imports_view),
    path('integration/v1/report-imports/<str:job_id>',integration_report_import_detail_view),
    path('integration/v1/report-imports/<str:job_id>/retry',integration_report_import_retry_view),
    path('integration/v1/assets/<str:ticket>',integration_asset_view),
    path('integration/v1/picker-sessions',integration_picker_session_view),
    path('integration/picker/<str:action>',picker_api_view),
    path('mcp',mcp_http_view),
    path('people/reference/<uuid:id>',reference_asset),
    path('api/<path:path>',api),
    path('media/<int:id>/<str:variant>',asset),
    path('exports/<int:id>',download_export),
    path('static/<str:name>',static_file),
]
