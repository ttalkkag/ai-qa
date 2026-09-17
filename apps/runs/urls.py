from django.urls import path

from . import views


app_name = "runs"
urlpatterns = [
    path("<uuid:run_id>/", views.detail, name="detail"),
    path("<uuid:run_id>/status/", views.status, name="status"),
    path("<uuid:run_id>/cancel/", views.cancel, name="cancel"),
    path("<uuid:run_id>/retry/", views.retry, name="retry"),
]
