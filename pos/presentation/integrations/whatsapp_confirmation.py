from __future__ import annotations

from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .whatsapp_confirmation_endpoint import handle_whatsapp_confirmation_request


@csrf_exempt
@require_POST
def confirmar_venta_whatsapp(request, venta_id: int):
    return handle_whatsapp_confirmation_request(request, venta_id)
