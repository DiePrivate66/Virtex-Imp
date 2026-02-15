from django.urls import path
from . import views

urlpatterns = [
    path('', views.menu_cliente, name='pedido_menu'),
    path('api/productos/', views.api_productos, name='pedido_api_productos'),
    path('api/crear/', views.api_crear_pedido, name='pedido_api_crear'),
    path('confirmacion/<int:pedido_id>/', views.confirmacion_pedido, name='pedido_confirmacion'),
]
