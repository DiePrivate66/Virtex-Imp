from __future__ import annotations

import logging
import json
from typing import Optional

from django.conf import settings
from django.utils import timezone

from twilio.base.exceptions import TwilioException
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from .models import WhatsAppMessageLog
from .whatsapp_utils import normalize_phone_to_e164

logger = logging.getLogger(__name__)


def validate_twilio_signature(request) -> bool:
    if not settings.TWILIO_SIGNATURE_VALIDATION:
        return True
    token = settings.TWILIO_AUTH_TOKEN
    signature = request.headers.get('X-Twilio-Signature', '')
    if not token or not signature:
        return False
    validator = RequestValidator(token)
    url = request.build_absolute_uri()
    form_data = request.POST.dict()
    return validator.validate(url, form_data, signature)


def _client() -> Client:
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def send_whatsapp_message(
    to_e164: str, body: str, status: str = 'queued', raise_on_error: bool = False
) -> Optional[str]:
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN or not settings.TWILIO_WHATSAPP_NUMBER:
        logger.warning('Twilio credentials missing; skipping outbound WhatsApp.')
        WhatsAppMessageLog.objects.create(
            direction='OUT',
            telefono_e164=to_e164,
            payload_json={'body': body, 'skipped': True},
            status='skipped',
        )
        return None

    from_number = settings.TWILIO_WHATSAPP_NUMBER
    if not from_number.startswith('whatsapp:'):
        from_number = f'whatsapp:{from_number}'

    to_number = to_e164
    if not to_number.startswith('+'):
        to_number = normalize_phone_to_e164(to_number)
    if not to_number.startswith('whatsapp:'):
        to_number = f'whatsapp:{to_number}'

    try:
        msg = _client().messages.create(from_=from_number, to=to_number, body=body)
        WhatsAppMessageLog.objects.create(
            direction='OUT',
            telefono_e164=normalize_phone_to_e164(to_e164),
            message_sid=msg.sid,
            payload_json={'body': body, 'to': to_number},
            status=msg.status or status,
        )
        return msg.sid
    except TwilioException as exc:
        logger.exception('Twilio outbound failed')
        WhatsAppMessageLog.objects.create(
            direction='OUT',
            telefono_e164=normalize_phone_to_e164(to_e164),
            payload_json={'body': body, 'error': str(exc)},
            status='failed',
        )
        if raise_on_error:
            raise
        return None


def build_twiml_response(body: str) -> str:
    response = MessagingResponse()
    response.message(body)
    return str(response)


def touch_conversation_inbound(conversation):
    conversation.last_inbound_at = timezone.now()
    conversation.save(update_fields=['last_inbound_at'])


def touch_conversation_outbound(conversation):
    conversation.last_outbound_at = timezone.now()
    conversation.save(update_fields=['last_outbound_at'])


def send_whatsapp_confirmation_buttons(
    to_e164: str,
    venta_id: int,
    total_productos: str,
    envio: str,
    total_ref: str,
    raise_on_error: bool = False,
):
    """Envia botones via Content Template si esta configurado en Twilio.
    Fallback: mensaje de texto para responder SI/NO.
    """
    content_sid = settings.TWILIO_CONFIRM_TEMPLATE_SID
    if not content_sid:
        body = (
            f'Pedido #{venta_id}\n'
            f'Productos: ${total_productos}\n'
            f'Envio: ${envio} (lo cobra el motorizado)\n'
            f'Total referencial: ${total_ref}\n\n'
            'Responde SI para confirmar o NO para cancelar.'
        )
        return send_whatsapp_message(to_e164, body, raise_on_error=raise_on_error)

    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN or not settings.TWILIO_WHATSAPP_NUMBER:
        return send_whatsapp_message(
            to_e164,
            'Responde SI para confirmar o NO para cancelar.'
        )

    from_number = settings.TWILIO_WHATSAPP_NUMBER
    if not from_number.startswith('whatsapp:'):
        from_number = f'whatsapp:{from_number}'

    to_number = to_e164
    if not to_number.startswith('+'):
        to_number = normalize_phone_to_e164(to_number)
    if not to_number.startswith('whatsapp:'):
        to_number = f'whatsapp:{to_number}'

    try:
        msg = _client().messages.create(
            from_=from_number,
            to=to_number,
            content_sid=content_sid,
            content_variables=json.dumps(
                {
                    '1': str(venta_id),
                    '2': str(total_productos),
                    '3': str(envio),
                    '4': str(total_ref),
                }
            )
        )
        WhatsAppMessageLog.objects.create(
            direction='OUT',
            telefono_e164=normalize_phone_to_e164(to_e164),
            message_sid=msg.sid,
            payload_json={'content_sid': content_sid, 'to': to_number},
            status=msg.status or 'queued',
        )
        return msg.sid
    except TwilioException:
        return send_whatsapp_message(
            to_e164,
            (
                f'Pedido #{venta_id}. Productos: ${total_productos}. Envio: ${envio}. '
                f'Total referencial: ${total_ref}. Responde SI o NO.'
            ),
            raise_on_error=raise_on_error,
        )
