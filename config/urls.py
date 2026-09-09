from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", LoginView.as_view(template_name="budgeting/login.html"), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", include("budgeting.urls")),
]
