from __future__ import annotations

import json
import logging
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from django.conf import settings

from pos.domain.shared import normalize_phone_to_e164
from pos.models import WhatsAppMessageLog

logger = logging.getLogger(__name__)


def _meta_graph_url() -> str:
    version = getattr(settings, 'META_WHATSAPP_API_VERSION', 'v22.0')
    phone_id = getattr(settings, 'META_WHATSAPP_PHONE_NUMBER_ID', '')
    return f'https://graph.facebook.com/{version}/{phone_id}/messages'


def _send_meta_payload(
    payload: dict, to_e164: str, status: str = 'queued', raise_on_error: bool = False
) -> Optional[str]:
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
        f'Total productos: ${total_productos}\n'
        f'Envio: ${envio} (lo pagas directo al repartidor)\n\n'
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
            f'Pedido #{venta_id}. Total productos: ${total_productos}. '
            f'Envio: ${envio}, pago directo al repartidor. Responde SI o NO.'
        ),
        raise_on_error=raise_on_error,
    )


def build_twiml_response(body: str) -> str:
    """Legacy helper kept for backward compatibility. Returns a simple XML ack."""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{body}</Message></Response>'
