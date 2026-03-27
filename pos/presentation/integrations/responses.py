from __future__ import annotations

from django.http import JsonResponse

from pos.application.integrations import IntegrationsError, WhatsAppIntegrationError


def integration_error_response(exc: IntegrationsError):
    return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)


def whatsapp_error_response(exc: WhatsAppIntegrationError):
    return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)
