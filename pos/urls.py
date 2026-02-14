from django.urls import path
from . import views

urlpatterns = [
    path('', views.pos_index, name='pos_index'),
    path('api/registrar-venta/', views.registrar_venta, name='registrar_venta'), # Nueva ruta
]