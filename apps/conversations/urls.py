from django.urls import path

from . import views

app_name = "conversations"
urlpatterns = [
    path("projects/<uuid:project_id>/conversations/", views.project_conversations, name="list"),
    path("conversations/<uuid:conversation_id>/", views.detail, name="detail"),
    path("conversations/<uuid:conversation_id>/messages/", views.message, name="message"),
    path("conversations/<uuid:conversation_id>/title/", views.rename, name="rename"),
    path("conversations/<uuid:conversation_id>/updates/", views.updates, name="updates"),
    path("conversations/<uuid:conversation_id>/responses/<uuid:call_id>/retry/", views.retry_response, name="retry_response"),
    path("conversations/<uuid:conversation_id>/request/", views.create_request, name="create_request"),
]
