from __future__ import annotations

import logging

from django.http import JsonResponse

from pos.application.web_orders import WebOrderError


def method_not_allowed_response():
    return JsonResponse({'status': 'error', 'mensaje': 'Metodo no permitido'}, status=405)


def web_order_created_response(venta, *, checkout_payload: dict | None = None):
    payload = {
        'status': 'ok',
        'pedido_id': venta.id,
        'mensaje': f'Pedido #{venta.id} recibido',
        'payment_status': venta.payment_status,
        'payment_status_display': venta.get_payment_status_display(),
        'payment_provider': venta.payment_provider,
        'confirmation_url': f'/pedido/confirmacion/{venta.id}/',
    }
    if checkout_payload:
        payload.update(checkout_payload)
    return JsonResponse(payload)


def web_order_error_response(exc: WebOrderError):
    return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)


def unexpected_web_order_error_response(logger: logging.Logger):
    logger.exception('Error inesperado creando pedido web')
    return JsonResponse(
        {'status': 'error', 'mensaje': 'No se pudo crear el pedido. Intenta nuevamente.'},
        status=500,
    )
