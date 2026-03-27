import logging

from django.views.decorators.csrf import csrf_exempt

from pos.application.web_orders import store_is_open

from .create_web_order_endpoint import handle_create_web_order_request
from .menu_endpoint import handle_menu_request
from .order_confirmation_endpoint import handle_order_confirmation_request
from .order_received_endpoint import handle_order_received_request
from .order_status_endpoint import handle_order_status_request
from .product_catalog_endpoint import handle_product_catalog_request

logger = logging.getLogger(__name__)


def esta_abierto():
    """Public facade for store-open checks used by the PWA flow."""
    return store_is_open()


def menu_cliente(request):
    """Render the public customer menu."""
    return handle_menu_request(request)


def api_productos(request):
    """Return the public product catalog payload."""
    return handle_product_catalog_request(request)


@csrf_exempt
def api_crear_pedido(request):
    """Create a public web order."""
    return handle_create_web_order_request(request, is_store_open=esta_abierto, logger=logger)


def confirmacion_pedido(request, pedido_id):
    """Render the public order confirmation page."""
    return handle_order_confirmation_request(request, pedido_id)


def api_estado_pedido(request, pedido_id):
    """Return public order status details for the PWA confirmation screen."""
    return handle_order_status_request(request, pedido_id)


def api_reportar_pedido_recibido(request, pedido_id):
    """Allow the customer to report a delivery as received."""
    return handle_order_received_request(request, pedido_id)


__all__ = [
    'api_crear_pedido',
    'api_estado_pedido',
    'api_productos',
    'api_reportar_pedido_recibido',
    'confirmacion_pedido',
    'esta_abierto',
    'menu_cliente',
]
