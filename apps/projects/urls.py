from django.urls import path

from . import views


app_name = "projects"
urlpatterns = [
    path("", views.project_list, name="list"),
    path("new/", views.project_create, name="create"),
    path("<uuid:project_id>/", views.project_detail, name="detail"),
    path("<uuid:project_id>/edit/", views.project_edit, name="edit"),
    path("<uuid:project_id>/configuration/", views.project_configuration, name="configure"),
    path("<uuid:project_id>/dependencies/", views.project_dependency, name="dependencies"),
    path("<uuid:project_id>/inspect/", views.project_inspect, name="inspect"),
    path("<uuid:project_id>/state/", views.project_state, name="state"),
]
