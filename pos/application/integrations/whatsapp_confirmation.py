from __future__ import annotations

from django.conf import settings

from pos.infrastructure.tasks import process_customer_confirmation

from .whatsapp_errors import WhatsAppIntegrationError


def queue_customer_confirmation(venta_id: int, decision: str, verify_key: str) -> None:
    expected = getattr(settings, 'WHATSAPP_WEBHOOK_VERIFY', '')
    if expected and verify_key != expected:
        raise WhatsAppIntegrationError('No autorizado', status_code=401)

    normalized_decision = (decision or '').upper().strip()
    if normalized_decision not in {'ACEPTADA', 'RECHAZADA'}:
        raise WhatsAppIntegrationError('Decision invalida', status_code=400)

    process_customer_confirmation.delay(venta_id, normalized_decision)
