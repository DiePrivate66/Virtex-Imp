from django.urls import path

from .public import (
    api_crear_pedido,
    api_estado_pedido,
    api_payphone_cancel,
    api_payphone_notify,
    api_payphone_return,
    api_productos,
    api_reportar_pedido_recibido,
    confirmacion_pedido,
    menu_cliente,
)

urlpatterns = [
    path('', menu_cliente, name='pedido_menu'),
    path('api/productos/', api_productos, name='pedido_api_productos'),
    path('api/crear/', api_crear_pedido, name='pedido_api_crear'),
    path('api/payphone/return/', api_payphone_return, name='pedido_api_payphone_return'),
    path('api/payphone/cancel/', api_payphone_cancel, name='pedido_api_payphone_cancel'),
    path('api/payphone/notify/', api_payphone_notify, name='pedido_api_payphone_notify'),
    path('api/pedidos/<int:pedido_id>/estado/', api_estado_pedido, name='pedido_api_estado'),
    path('api/pedidos/<int:pedido_id>/recibido/', api_reportar_pedido_recibido, name='pedido_api_recibido'),
    path('confirmacion/<int:pedido_id>/', confirmacion_pedido, name='pedido_confirmacion'),
]
