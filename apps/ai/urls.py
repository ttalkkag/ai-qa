from django.urls import path

from . import views


app_name = "ai"
urlpatterns = [
    path("", views.settings_view, name="settings"),
    path("connection/", views.connection_view, name="connection"),
    path("profiles/save/", views.save_profile_view, name="save-profile"),
    path("profiles/<uuid:profile_id>/validate/", views.validate_profile_view, name="validate-profile"),
    path("sandbox/", views.sandbox_view, name="sandbox"),
]
