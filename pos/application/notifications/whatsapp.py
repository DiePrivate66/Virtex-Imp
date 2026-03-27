from __future__ import annotations

from typing import Optional

from pos.infrastructure.notifications import (
    build_twiml_response as _build_twiml_response,
    extract_inbound_whatsapp as _extract_inbound_whatsapp,
    send_whatsapp_confirmation_buttons as _send_whatsapp_confirmation_buttons,
    send_whatsapp_message as _send_whatsapp_message,
    touch_conversation_inbound as _touch_conversation_inbound,
    touch_conversation_outbound as _touch_conversation_outbound,
    validate_meta_signature as _validate_meta_signature,
    validate_whatsapp_signature as _validate_whatsapp_signature,
)


def validate_inbound_whatsapp_request(request) -> bool:
    return _validate_whatsapp_signature(request)


def validate_meta_whatsapp_request(request) -> bool:
    return _validate_meta_signature(request)


def extract_inbound_whatsapp_request(request) -> Optional[dict]:
    return _extract_inbound_whatsapp(request)


def send_whatsapp_text(
    phone_e164: str, body: str, status: str = 'queued', raise_on_error: bool = False
):
    return _send_whatsapp_message(phone_e164, body, status, raise_on_error)


def send_whatsapp_confirmation_request(
    to_e164: str,
    venta_id: int,
    total_productos: str,
    envio: str,
    total_ref: str,
    raise_on_error: bool = False,
):
    return _send_whatsapp_confirmation_buttons(
        to_e164=to_e164,
        venta_id=venta_id,
        total_productos=total_productos,
        envio=envio,
        total_ref=total_ref,
        raise_on_error=raise_on_error,
    )


def build_whatsapp_twiml_response(body: str) -> str:
    return _build_twiml_response(body)


def touch_whatsapp_conversation_inbound(conversation):
    return _touch_conversation_inbound(conversation)


def touch_whatsapp_conversation_outbound(conversation):
    return _touch_conversation_outbound(conversation)
