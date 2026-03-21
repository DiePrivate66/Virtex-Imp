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

from .models import WhatsAppMessageLog
from .whatsapp_utils import normalize_phone_to_e164

logger = logging.getLogger(__name__)


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
    return validate_meta_signature(request)


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


def send_whatsapp_message(
    to_e164: str, body: str, status: str = 'queued', raise_on_error: bool = False
) -> Optional[str]:
    to_number = to_e164 if to_e164.startswith('+') else normalize_phone_to_e164(to_e164)
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_number.replace('+', ''),
        'type': 'text',
        'text': {'body': body},
    }
    return _send_meta_payload(payload, to_e164=to_number, status=status, raise_on_error=raise_on_error)


def build_twiml_response(body: str) -> str:
    """Legacy helper kept for backward compatibility. Returns a simple XML ack."""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{body}</Message></Response>'


def touch_conversation_inbound(conversation):
    conversation.last_inbound_at = timezone.now()
    conversation.save(update_fields=['last_inbound_at'])


def touch_conversation_outbound(conversation):
    conversation.last_outbound_at = timezone.now()
    conversation.save(update_fields=['last_outbound_at'])


def extract_inbound_whatsapp(request) -> Optional[dict]:
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


def send_whatsapp_confirmation_buttons(
    to_e164: str,
    venta_id: int,
    total_productos: str,
    envio: str,
    total_ref: str,
    raise_on_error: bool = False,
):
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
