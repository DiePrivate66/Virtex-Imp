from __future__ import annotations

from django.http import JsonResponse

from pos.application.notifications import (
    extract_inbound_whatsapp_request,
    validate_inbound_whatsapp_request,
)
from pos.models import WhatsAppMessageLog


def _log_rejected_webhook(request, reason: str):
    try:
        body_preview = (request.body or b'')[:1000].decode('utf-8', errors='replace')
        WhatsAppMessageLog.objects.create(
            direction='IN',
            telefono_e164='unknown',
            payload_json={
                'reason': reason,
                'signature_present': bool(request.headers.get('X-Hub-Signature-256')),
                'content_type': request.headers.get('Content-Type', ''),
                'body_preview': body_preview,
            },
            status='failed',
        )
    except Exception:
        # The webhook response must stay deterministic even if diagnostic logging fails.
        pass


def get_validated_whatsapp_inbound(request):
    if not validate_inbound_whatsapp_request(request):
        _log_rejected_webhook(request, 'invalid_signature')
        return None, JsonResponse({'status': 'error', 'mensaje': 'invalid signature'}, status=403)

    inbound = extract_inbound_whatsapp_request(request)
    if not inbound:
        return None, JsonResponse({'status': 'ok', 'mensaje': 'no inbound message'})

    return inbound, None
