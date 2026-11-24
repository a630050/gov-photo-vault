from django.urls import path
from . import views
from transfer_common.ops_django import live,ready
urlpatterns=[path('health/live',live),path('health/ready',ready),path('',views.home),path('api/<str:path>',views.api),path('preview/<uuid:id>',views.preview),path('bridge/<path:path>',views.bridge),path('static/<str:name>',views.static_file)]
