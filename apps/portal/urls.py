from django.urls import path

from . import views


app_name = "portal"
urlpatterns = [
    path("admin/", views.admin, name="admin"),
    path("docs/", views.docs, name="docs"),
]
