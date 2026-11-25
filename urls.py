from django.urls import path
from django.http import FileResponse,Http404
from pathlib import Path
from core.views import home,api,asset,download_export
from core.people import reference_asset
from transfer_common.ops_django import live,ready,status
BASE=Path(__file__).parent
def static_file(request,name):
    if name not in ['app.js','compare.js','similarity.js','content-search.js','people.js','external.js','library.js','workflow.js','upload.js','admin.js','jobs.js','collections.js','style.css','leaflet.js','leaflet.css']:raise Http404
    return FileResponse((BASE/'static'/name).open('rb'),content_type='text/javascript' if name.endswith('.js') else 'text/css')
urlpatterns=[path('health/live',live),path('health/ready',ready),path('ops/status',status),path('',home),path('people/reference/<uuid:id>',reference_asset),path('api/<path:path>',api),path('media/<int:id>/<str:variant>',asset),path('exports/<int:id>',download_export),path('static/<str:name>',static_file)]
