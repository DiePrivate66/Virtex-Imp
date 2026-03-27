from django.urls import path

from .public import (
    api_crear_pedido,
    api_estado_pedido,
    api_productos,
    api_reportar_pedido_recibido,
    confirmacion_pedido,
    menu_cliente,
)

urlpatterns = [
    path('', menu_cliente, name='pedido_menu'),
    path('api/productos/', api_productos, name='pedido_api_productos'),
    path('api/crear/', api_crear_pedido, name='pedido_api_crear'),
    path('api/pedidos/<int:pedido_id>/estado/', api_estado_pedido, name='pedido_api_estado'),
    path('api/pedidos/<int:pedido_id>/recibido/', api_reportar_pedido_recibido, name='pedido_api_recibido'),
    path('confirmacion/<int:pedido_id>/', confirmacion_pedido, name='pedido_confirmacion'),
]
