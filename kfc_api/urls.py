from django.contrib import admin
from django.urls import path,include

urlpatterns = [
    path('api/auth/', include('authentication.urls')),
    path('admin/', admin.site.urls),
    path("", include("menu.urls")),
    path("appointment/", include("appointment.urls")),
    path("voice/", include("voice.urls")),
    path("whatsapp/", include("whatsapp.urls")),
]
