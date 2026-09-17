from django.urls import path

from . import views


app_name = "artifacts"
urlpatterns = [
    path("results/<uuid:bundle_id>/", views.result_detail, name="detail"),
    path("results/<uuid:bundle_id>/review/", views.review_result, name="review"),
    path("artifacts/<uuid:artifact_id>/download/", views.download, name="download"),
    path("conversations/<uuid:conversation_id>/attachments/", views.upload, name="upload"),
]
