# authentication/urls.py
from django.urls import path
from .views import (RegisterView,LoginView)

urlpatterns = [
    path('register/',        RegisterView.as_view(),       name='auth-register'),
    path('login/',           LoginView.as_view(),           name='auth-login'),
    # path('google/',          GoogleAuthView.as_view(),      name='auth-google'),
    # path('logout/',          LogoutView.as_view(),          name='auth-logout'),
    # path('me/',              MeView.as_view(),              name='auth-me'),
    # path('change-password/', ChangePasswordView.as_view(),  name='auth-change-password'),
]