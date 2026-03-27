from __future__ import annotations

from django.http import JsonResponse

from pos.application.integrations import WhatsAppIntegrationError, queue_customer_confirmation

from .payloads import parse_json_body
from .whatsapp_common import whatsapp_error_response


def handle_whatsapp_confirmation_request(request, venta_id: int):
    data = parse_json_body(request)

    try:
        queue_customer_confirmation(
            venta_id=venta_id,
            decision=data.get('decision') or '',
            verify_key=request.headers.get('X-Webhook-Verify', ''),
        )
        return JsonResponse({'status': 'ok'})
    except WhatsAppIntegrationError as exc:
        return whatsapp_error_response(exc)
