from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.home, name="home"),
    path("signup/", views.signup, name="signup"),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("stock/<str:stock_id>/", views.stock_detail, name="stock_detail"),
    path("stock/<str:stock_id>/favorite/", views.toggle_favorite, name="toggle_favorite"),
    path("stock/<str:stock_id>/refresh/", views.refresh_price, name="refresh_price"),
    path("favorites/", views.favorites, name="favorites"),
    path("screener/", views.screener, name="screener"),
]
