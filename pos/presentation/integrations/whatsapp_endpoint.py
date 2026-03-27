from __future__ import annotations

import logging

from django.http import JsonResponse

from pos.application.integrations import (
    WhatsAppIntegrationError,
    handle_inbound_whatsapp,
    verify_meta_webhook_subscription,
)

from .whatsapp_common import whatsapp_error_response
from .whatsapp_requests import get_validated_whatsapp_inbound
from .whatsapp_responses import webhook_ack_response, webhook_challenge_response

logger = logging.getLogger(__name__)


def handle_whatsapp_webhook_request(request):
    if request.method == 'GET':
        return _handle_whatsapp_webhook_verification(request)
    return _handle_whatsapp_webhook_inbound(request)


def _handle_whatsapp_webhook_verification(request):
    try:
        challenge = verify_meta_webhook_subscription(
            request.GET.get('hub.mode', ''),
            request.GET.get('hub.verify_token', ''),
            request.GET.get('hub.challenge', ''),
        )
        return webhook_challenge_response(challenge)
    except WhatsAppIntegrationError as exc:
        return whatsapp_error_response(exc)


def _handle_whatsapp_webhook_inbound(request):
    inbound, error_response = get_validated_whatsapp_inbound(request)
    if error_response:
        return error_response

    try:
        ack = handle_inbound_whatsapp(inbound)
        return webhook_ack_response(ack.phone_e164, ack.body)
    except WhatsAppIntegrationError as exc:
        return whatsapp_error_response(exc)
    except Exception:
        logger.exception('Error inesperado procesando webhook de WhatsApp')
        return JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo procesar el webhook. Intenta nuevamente.'},
            status=500,
        )
