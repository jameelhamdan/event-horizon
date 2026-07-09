from django.contrib import admin
from django.urls import include, path, re_path

from app.views import flower

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('api.urls')),
    re_path(r'^flower/(?P<path>.*)$', flower, name='flower'),
]
