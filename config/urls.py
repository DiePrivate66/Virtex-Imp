from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    # Esto habilita el panel de administración
    path('admin/', admin.site.urls),
    
    # IMPORTANTE: Aquí decimos "Cuando entres a la raíz, usa las rutas de la app POS"
    path('', include('pos.urls')),
]

# Esto es un truco para que las imágenes funcionen mientras desarrollamos
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)