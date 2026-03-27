from __future__ import annotations

from django.http import JsonResponse

from pos.application.web_orders import WebOrderError, create_web_order, get_closed_store_message

from .web_order_requests import parse_web_order_request
from .web_order_responses import (
    method_not_allowed_response,
    unexpected_web_order_error_response,
    web_order_created_response,
    web_order_error_response,
)


def handle_create_web_order_request(request, *, is_store_open, logger):
    """Orquesta la creacion HTTP de un pedido web."""
    if request.method != 'POST':
        return method_not_allowed_response()

    if not is_store_open():
        return JsonResponse({'status': 'error', 'mensaje': get_closed_store_message()}, status=400)

    try:
        data, comprobante = parse_web_order_request(request)
        venta = create_web_order(data, comprobante=comprobante)
        return web_order_created_response(venta)
    except WebOrderError as exc:
        return web_order_error_response(exc)
    except Exception:
        return unexpected_web_order_error_response(logger)
