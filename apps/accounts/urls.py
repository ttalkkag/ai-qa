from django.urls import path

from . import views

app_name = "accounts"
urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("password/", views.password_view, name="password"),
    path("me/", views.me_view, name="me"),
    path("manage/", views.manage_view, name="manage"),
]
