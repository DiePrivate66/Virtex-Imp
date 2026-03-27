from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from pos.domain.shared import normalize_phone_to_e164
from pos.domain.web_orders import parse_customer_confirmation
from pos.infrastructure.tasks import process_customer_confirmation
from pos.models import WhatsAppConversation, WhatsAppMessageLog

from .whatsapp_errors import WhatsAppWebhookAck


def _is_whatsapp_rate_limited(phone_e164: str) -> bool:
    key = f'wa:inbound:rl:{phone_e164}'
    window = max(1, int(getattr(settings, 'WHATSAPP_INBOUND_RATE_LIMIT_WINDOW_SECONDS', 60)))
    max_events = max(1, int(getattr(settings, 'WHATSAPP_INBOUND_RATE_LIMIT_MAX', 20)))
    try:
        if cache.add(key, 1, timeout=window):
            return False
        count = cache.incr(key)
        return int(count) > max_events
    except Exception:
        recent_count = WhatsAppMessageLog.objects.filter(
            direction='IN',
            telefono_e164=phone_e164,
            created_at__gte=timezone.now() - timedelta(seconds=window),
        ).count()
        return recent_count > max_events


def _build_default_link_message() -> str:
    return (
        'Hola. Bienvenido a RAMON by Bosco.\n'
        f'Haz tu pedido aqui: {settings.PUBLIC_PWA_URL}\n\n'
        'Cuando termines te contactaremos por este chat para confirmar envio si aplica.'
    )


def handle_inbound_whatsapp(inbound: dict) -> WhatsAppWebhookAck:
    from_raw = inbound.get('from_raw', '')
    body = (inbound.get('body') or '').strip()
    button_text = (inbound.get('button_text') or '').strip()
    button_payload = (inbound.get('button_payload') or '').strip()
    message_sid = inbound.get('message_sid')

    phone_e164 = normalize_phone_to_e164(from_raw)

    if message_sid and WhatsAppMessageLog.objects.filter(message_sid=message_sid).exists():
        return WhatsAppWebhookAck(phone_e164=phone_e164, body='Mensaje ya recibido.')

    if _is_whatsapp_rate_limited(phone_e164):
        WhatsAppMessageLog.objects.create(
            direction='IN',
            telefono_e164=phone_e164,
            payload_json={'reason': 'rate_limited', 'raw': inbound.get('raw_payload') or {}},
            status='rate_limited',
        )
        return WhatsAppWebhookAck(
            phone_e164=phone_e164,
            body='Demasiados mensajes en poco tiempo. Intenta en 1 minuto.',
        )

    WhatsAppMessageLog.objects.create(
        direction='IN',
        telefono_e164=phone_e164,
        message_sid=message_sid,
        payload_json=inbound.get('raw_payload') or {},
        status='received',
    )

    conv, _ = WhatsAppConversation.objects.get_or_create(telefono_e164=phone_e164)
    conv.last_inbound_at = timezone.now()

    inbound_text = button_payload or button_text or body
    decision = parse_customer_confirmation(inbound_text)
    if conv.estado_flujo == 'ESPERANDO_CONFIRMACION_TOTAL' and conv.venta_id:
        if decision in {'ACEPTADA', 'RECHAZADA'}:
            process_customer_confirmation.delay(conv.venta_id, decision)
            conv.estado_flujo = 'FINALIZADO'
            conv.save(update_fields=['estado_flujo', 'last_inbound_at'])
            return WhatsAppWebhookAck(
                phone_e164=phone_e164,
                body='Perfecto. Tu respuesta fue registrada.',
            )

        conv.save(update_fields=['last_inbound_at'])
        return WhatsAppWebhookAck(
            phone_e164=phone_e164,
            body='Por favor responde SI para confirmar o NO para cancelar.',
        )

    conv.estado_flujo = 'LINK_ENVIADO'
    conv.save(update_fields=['estado_flujo', 'last_inbound_at'])
    return WhatsAppWebhookAck(phone_e164=phone_e164, body=_build_default_link_message())
