from django.urls import path

from . import views

app_name = "approvals"
urlpatterns = [
    path("approvals/", views.list_view, name="list"),
    path("approvals/jobs/<uuid:job_id>/", views.preparation_status_view, name="preparation-status"),
    path("projects/<uuid:project_id>/publication-policy/", views.policy_view, name="policy"),
    path("runs/<uuid:run_id>/publication/prepare/", views.prepare_view, name="prepare"),
    path("approvals/<uuid:publication_id>/", views.detail_view, name="detail"),
    path("approvals/<uuid:publication_id>/<str:action>/", views.action_view, name="action"),
]
