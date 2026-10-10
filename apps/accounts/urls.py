from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("signup/", views.SignUpView.as_view(), name="signup"),
    path("users/<int:pk>/activate/", views.set_user_active, name="set_user_active"),
    path("users/<int:pk>/delete/", views.delete_user, name="delete_user"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("ui/theme/", views.toggle_theme, name="toggle_theme"),
    path("ui/language/", views.toggle_language, name="toggle_language"),
]
