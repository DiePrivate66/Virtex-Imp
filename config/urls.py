from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # Panel de administracion.
    path('admin/', admin.site.urls),

    # PWA publica de pedidos. La tabla canonica vive en presentation.
    path('pedido/', include('pos.presentation.api.urls')),

    # POS interno y resto del bounded context.
    path('', include('pos.urls')),
]

# Media solo en desarrollo.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
