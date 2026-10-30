"""URL routing: two Ninja APIs with separate auth boundaries."""

from django.urls import path

from .api import app_api, ext_api

urlpatterns = [
    path("api/extension/", ext_api.urls),
    path("api/", app_api.urls),
]
