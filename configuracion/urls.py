from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    # CORRECCIÓN: Se utiliza admin.site.urls
    path('admin/', admin.site.urls),
    
    # Incluimos las rutas de nucleo_sistema
    path('', include('nucleo_sistema.urls')),
]