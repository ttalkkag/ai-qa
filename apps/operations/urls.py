from django.urls import path

from . import views


app_name = "operations"
urlpatterns = [path("", views.dashboard, name="dashboard")]
