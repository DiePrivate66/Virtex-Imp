from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from django.conf import settings
from django.utils import timezone

from twilio.base.exceptions import TwilioException
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from .models import WhatsAppMessageLog
from .whatsapp_utils import normalize_phone_to_e164

logger = logging.getLogger(__name__)


def get_whatsapp_provider() -> str:
    return (getattr(settings, 'WHATSAPP_PROVIDER', 'TWILIO') or 'TWILIO').upper()


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


def validate_meta_signature(request) -> bool:
    if not getattr(settings, 'META_SIGNATURE_VALIDATION', False):
        return True
    app_secret = getattr(settings, 'META_WHATSAPP_APP_SECRET', '')
    if not app_secret:
        return False
    signature = request.headers.get('X-Hub-Signature-256', '')
    if not signature.startswith('sha256='):
        return False
    expected = hmac.new(app_secret.encode('utf-8'), request.body, hashlib.sha256).hexdigest()
    received = signature.split('=', 1)[1].strip()
    return hmac.compare_digest(expected, received)


def validate_whatsapp_signature(request) -> bool:
    provider = get_whatsapp_provider()
    if provider == 'META':
        return validate_meta_signature(request)
    return validate_twilio_signature(request)


def _twilio_client() -> Client:
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def _meta_graph_url() -> str:
    version = getattr(settings, 'META_WHATSAPP_API_VERSION', 'v22.0')
    phone_id = getattr(settings, 'META_WHATSAPP_PHONE_NUMBER_ID', '')
    return f'https://graph.facebook.com/{version}/{phone_id}/messages'


def _send_meta_payload(payload: dict, to_e164: str, status: str = 'queued', raise_on_error: bool = False) -> Optional[str]:
    token = getattr(settings, 'META_WHATSAPP_TOKEN', '')
    phone_id = getattr(settings, 'META_WHATSAPP_PHONE_NUMBER_ID', '')
    if not token or not phone_id:
        logger.warning('Meta WhatsApp credentials missing; skipping outbound WhatsApp.')
        WhatsAppMessageLog.objects.create(
            direction='OUT',
            telefono_e164=to_e164,
            payload_json={'payload': payload, 'skipped': True, 'provider': 'META'},
            status='skipped',
        )
        return None

    req = urlrequest.Request(
        _meta_graph_url(),
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            response_raw = resp.read().decode('utf-8')
            response_json = json.loads(response_raw or '{}')
            msg_id = ((response_json.get('messages') or [{}])[0]).get('id')
            WhatsAppMessageLog.objects.create(
                direction='OUT',
                telefono_e164=normalize_phone_to_e164(to_e164),
                message_sid=msg_id,
                payload_json={'payload': payload, 'response': response_json, 'provider': 'META'},
                status=status,
            )
            return msg_id
    except Exception as exc:
        body = str(exc)
        if isinstance(exc, urlerror.HTTPError):
            try:
                body = exc.read().decode('utf-8')
            except Exception:
                body = str(exc)
        logger.exception('Meta outbound failed')
        WhatsAppMessageLog.objects.create(
            direction='OUT',
            telefono_e164=normalize_phone_to_e164(to_e164),
            payload_json={'payload': payload, 'error': body, 'provider': 'META'},
            status='failed',
        )
        if raise_on_error:
            raise
        return None


def _send_via_twilio(to_e164: str, body: str, status: str = 'queued', raise_on_error: bool = False) -> Optional[str]:
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN or not settings.TWILIO_WHATSAPP_NUMBER:
        logger.warning('Twilio credentials missing; skipping outbound WhatsApp.')
        WhatsAppMessageLog.objects.create(
            direction='OUT',
            telefono_e164=to_e164,
            payload_json={'body': body, 'skipped': True, 'provider': 'TWILIO'},
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
        msg = _twilio_client().messages.create(from_=from_number, to=to_number, body=body)
        WhatsAppMessageLog.objects.create(
            direction='OUT',
            telefono_e164=normalize_phone_to_e164(to_e164),
            message_sid=msg.sid,
            payload_json={'body': body, 'to': to_number, 'provider': 'TWILIO'},
            status=msg.status or status,
        )
        return msg.sid
    except TwilioException as exc:
        logger.exception('Twilio outbound failed')
        WhatsAppMessageLog.objects.create(
            direction='OUT',
            telefono_e164=normalize_phone_to_e164(to_e164),
            payload_json={'body': body, 'error': str(exc), 'provider': 'TWILIO'},
            status='failed',
        )
        if raise_on_error:
            raise
        return None


def _send_via_meta_text(to_e164: str, body: str, status: str = 'queued', raise_on_error: bool = False) -> Optional[str]:
    to_number = to_e164 if to_e164.startswith('+') else normalize_phone_to_e164(to_e164)
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_number.replace('+', ''),
        'type': 'text',
        'text': {'body': body},
    }
    return _send_meta_payload(payload, to_e164=to_number, status=status, raise_on_error=raise_on_error)


def send_whatsapp_message(
    to_e164: str, body: str, status: str = 'queued', raise_on_error: bool = False
) -> Optional[str]:
    provider = get_whatsapp_provider()
    if provider == 'META':
        return _send_via_meta_text(to_e164, body, status=status, raise_on_error=raise_on_error)
    return _send_via_twilio(to_e164, body, status=status, raise_on_error=raise_on_error)


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


def extract_inbound_whatsapp(request) -> Optional[dict]:
    provider = get_whatsapp_provider()
    if provider == 'META':
        try:
            payload = json.loads(request.body or '{}')
        except Exception:
            return None

        for entry in payload.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {}) or {}
                messages = value.get('messages') or []
                for msg in messages:
                    from_raw = msg.get('from', '')
                    message_id = msg.get('id')
                    msg_type = msg.get('type')
                    body = ''
                    button_text = ''
                    button_payload = ''

                    if msg_type == 'text':
                        body = (msg.get('text') or {}).get('body', '') or ''
                    elif msg_type == 'button':
                        btn = msg.get('button') or {}
                        button_text = btn.get('text', '') or ''
                        button_payload = btn.get('payload', '') or ''
                    elif msg_type == 'interactive':
                        interactive = msg.get('interactive') or {}
                        itype = interactive.get('type')
                        if itype == 'button_reply':
                            reply = interactive.get('button_reply') or {}
                            button_text = reply.get('title', '') or ''
                            button_payload = reply.get('id', '') or ''
                        elif itype == 'list_reply':
                            reply = interactive.get('list_reply') or {}
                            button_text = reply.get('title', '') or ''
                            button_payload = reply.get('id', '') or ''

                    return {
                        'from_raw': from_raw,
                        'body': body.strip(),
                        'button_text': button_text.strip(),
                        'button_payload': button_payload.strip(),
                        'message_sid': message_id,
                        'raw_payload': payload,
                    }
        return None

    return {
        'from_raw': request.POST.get('From', ''),
        'body': (request.POST.get('Body', '') or '').strip(),
        'button_text': (request.POST.get('ButtonText', '') or '').strip(),
        'button_payload': (request.POST.get('ButtonPayload', '') or '').strip(),
        'message_sid': request.POST.get('MessageSid'),
        'raw_payload': request.POST.dict(),
    }


def send_whatsapp_confirmation_buttons(
    to_e164: str,
    venta_id: int,
    total_productos: str,
    envio: str,
    total_ref: str,
    raise_on_error: bool = False,
):
    provider = get_whatsapp_provider()
    if provider == 'META':
        to_number = to_e164 if to_e164.startswith('+') else normalize_phone_to_e164(to_e164)
        body_text = (
            f'Pedido #{venta_id}\n'
            f'Productos: ${total_productos}\n'
            f'Envio: ${envio} (lo cobra el motorizado)\n'
            f'Total referencial: ${total_ref}\n\n'
            'Confirma tu pedido:'
        )
        payload = {
            'messaging_product': 'whatsapp',
            'to': to_number.replace('+', ''),
            'type': 'interactive',
            'interactive': {
                'type': 'button',
                'body': {'text': body_text},
                'action': {
                    'buttons': [
                        {'type': 'reply', 'reply': {'id': 'CONFIRMAR_SI', 'title': 'Confirmar'}},
                        {'type': 'reply', 'reply': {'id': 'CONFIRMAR_NO', 'title': 'Cancelar'}},
                    ]
                },
            },
        }
        sid = _send_meta_payload(payload, to_e164=to_number, raise_on_error=raise_on_error)
        if sid:
            return sid
        return send_whatsapp_message(
            to_number,
            (
                f'Pedido #{venta_id}. Productos: ${total_productos}. Envio: ${envio}. '
                f'Total referencial: ${total_ref}. Responde SI o NO.'
            ),
            raise_on_error=raise_on_error,
        )

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
        return send_whatsapp_message(to_e164, 'Responde SI para confirmar o NO para cancelar.')

    from_number = settings.TWILIO_WHATSAPP_NUMBER
    if not from_number.startswith('whatsapp:'):
        from_number = f'whatsapp:{from_number}'

    to_number = to_e164
    if not to_number.startswith('+'):
        to_number = normalize_phone_to_e164(to_number)
    if not to_number.startswith('whatsapp:'):
        to_number = f'whatsapp:{to_number}'

    try:
        msg = _twilio_client().messages.create(
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
            ),
        )
        WhatsAppMessageLog.objects.create(
            direction='OUT',
            telefono_e164=normalize_phone_to_e164(to_e164),
            message_sid=msg.sid,
            payload_json={'content_sid': content_sid, 'to': to_number, 'provider': 'TWILIO'},
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
