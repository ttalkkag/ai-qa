from django.urls import path

from . import views

app_name = "work_requests"
urlpatterns = [
    path("requests/", views.index, name="list"),
    path("requests/<uuid:request_id>/", views.detail, name="detail"),
    path("requests/<uuid:request_id>/progress/", views.progress, name="progress"),
    path("requests/<uuid:request_id>/draft/", views.save_draft, name="draft"),
    path("requests/<uuid:request_id>/refresh-basis/", views.refresh_basis, name="refresh_basis"),
    path("requests/<uuid:request_id>/clarify/", views.clarify, name="clarify"),
    path("requests/<uuid:request_id>/suggestions/<uuid:suggestion_id>/apply/", views.apply_suggestion, name="apply_suggestion"),
    path("requests/<uuid:request_id>/ready/", views.ready, name="ready"),
    path("requests/<uuid:request_id>/submit/", views.submit, name="submit"),
]
