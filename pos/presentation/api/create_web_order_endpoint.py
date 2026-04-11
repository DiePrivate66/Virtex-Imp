from __future__ import annotations

from django.http import JsonResponse

from pos.application.web_orders import (
    WebOrderError,
    cancel_payphone_web_order,
    create_web_order,
    get_closed_store_message,
    prepare_payphone_checkout_for_web_order,
)

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
        checkout_payload = None
        if venta.metodo_pago == 'PAYPHONE':
            try:
                checkout_payload = prepare_payphone_checkout_for_web_order(
                    venta,
                    remote_ip=_extract_remote_ip(request),
                )
            except WebOrderError:
                cancel_payphone_web_order(venta, reason='No se pudo iniciar el checkout de PayPhone')
                raise
        return web_order_created_response(venta, checkout_payload=checkout_payload)
    except WebOrderError as exc:
        return web_order_error_response(exc)
    except Exception:
        return unexpected_web_order_error_response(logger)


def _extract_remote_ip(request) -> str:
    forwarded_for = str(request.META.get('HTTP_X_FORWARDED_FOR') or '').strip()
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return str(request.META.get('REMOTE_ADDR') or '').strip()
