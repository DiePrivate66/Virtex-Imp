from __future__ import annotations

from django.http import JsonResponse

from pos.application.notifications import (
    extract_inbound_whatsapp_request,
    validate_inbound_whatsapp_request,
)


def get_validated_whatsapp_inbound(request):
    if not validate_inbound_whatsapp_request(request):
        return None, JsonResponse({'status': 'error', 'mensaje': 'invalid signature'}, status=403)

    inbound = extract_inbound_whatsapp_request(request)
    if not inbound:
        return None, JsonResponse({'status': 'ok', 'mensaje': 'no inbound message'})

    return inbound, None
