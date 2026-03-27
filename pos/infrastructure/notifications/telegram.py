from __future__ import annotations

import json
import logging
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from django.conf import settings

logger = logging.getLogger(__name__)


def _telegram_api_url(method: str) -> str:
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    return f'https://api.telegram.org/bot{token}/{method}'


def _send_telegram(method: str, payload: dict) -> Optional[dict]:
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    if not token:
        logger.debug('TELEGRAM_BOT_TOKEN not configured; skipping.')
        return None

    req = urlrequest.Request(
        _telegram_api_url(method),
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urlerror.HTTPError as exc:
        body = ''
        try:
            body = exc.read().decode('utf-8')
        except Exception:
            body = str(exc)
        logger.exception('Telegram API error: %s', body)
        return None
    except Exception:
        logger.exception('Telegram request failed')
        return None


def notify_delivery_group(venta) -> Optional[int]:
    """Send order details + claim link + GPS location to Telegram group."""
    chat_id = getattr(settings, 'TELEGRAM_DELIVERY_GROUP_ID', '')
    if not chat_id:
        logger.debug('TELEGRAM_DELIVERY_GROUP_ID not configured; skipping.')
        return None

    from pos.infrastructure.delivery import make_delivery_claim_token

    claim_token = make_delivery_claim_token(venta.id)
    claim_url = f'{settings.PUBLIC_BACKEND_URL}/integrations/delivery/claim/{claim_token}/'

    map_url = ''
    if venta.ubicacion_lat is not None and venta.ubicacion_lng is not None:
        map_url = f'https://www.google.com/maps?q={venta.ubicacion_lat},{venta.ubicacion_lng}'

    items_text = ''
    for detalle in venta.detalles.select_related('producto').all():
        nota = f' ({detalle.nota})' if detalle.nota else ''
        items_text += f'  - {detalle.cantidad}x {detalle.producto.nombre}{nota}\n'

    text = (
        f'Nuevo Pedido #{venta.id}\n'
        f'Cliente: {venta.cliente_nombre}\n'
        f'Telefono: {venta.telefono_cliente or "No proporcionado"}\n'
        f'Direccion: {venta.direccion_envio or "Sin direccion"}\n'
        f'Mapa: {map_url or "Ubicacion no compartida"}\n\n'
        f'Detalle:\n{items_text or "  (sin items)"}\n'
        f'Total: ${venta.total:.2f}\n'
        f'Pago: {venta.get_metodo_pago_display()}\n\n'
        f'Aceptar pedido: {claim_url}'
    )

    result = _send_telegram(
        'sendMessage',
        {
            'chat_id': chat_id,
            'text': text,
            'disable_web_page_preview': True,
        },
    )

    message_id = None
    if result and result.get('ok'):
        message_id = result.get('result', {}).get('message_id')

    if result and venta.ubicacion_lat is not None and venta.ubicacion_lng is not None:
        _send_telegram(
            'sendLocation',
            {
                'chat_id': chat_id,
                'latitude': venta.ubicacion_lat,
                'longitude': venta.ubicacion_lng,
                'reply_to_message_id': message_id,
            },
        )

    return message_id


def notify_order_claimed(venta, empleado, precio_envio=None, reply_to_message_id: Optional[int] = None) -> bool:
    """Notify the Telegram group that an order was claimed by a driver."""
    chat_id = getattr(settings, 'TELEGRAM_DELIVERY_GROUP_ID', '')
    if not chat_id:
        return False

    from pos.infrastructure.delivery import make_delivery_in_transit_token

    in_transit_token = make_delivery_in_transit_token(venta.id, empleado.id)
    in_transit_url = f'{settings.PUBLIC_BACKEND_URL}/integrations/delivery/in-transit/{in_transit_token}/'

    shipping_amount = precio_envio if precio_envio is not None else venta.costo_envio

    text = (
        f'Pedido #{venta.id} tomado por {empleado.nombre}\n'
        f'Envio: ${shipping_amount:.2f}\n'
        f'Marcar en camino: {in_transit_url}'
    )

    payload = {
        'chat_id': chat_id,
        'text': text,
    }
    if reply_to_message_id:
        payload['reply_to_message_id'] = reply_to_message_id

    result = _send_telegram('sendMessage', payload)
    return result is not None


def notify_customer_reported_received(venta, empleado) -> bool:
    """Notify the delivery group that the customer reported the order as received."""
    chat_id = getattr(settings, 'TELEGRAM_DELIVERY_GROUP_ID', '')
    if not chat_id:
        return False

    from pos.infrastructure.delivery import make_delivery_delivered_token

    delivered_token = make_delivery_delivered_token(venta.id, empleado.id)
    delivered_url = f'{settings.PUBLIC_BACKEND_URL}/integrations/delivery/delivered/{delivered_token}/'

    text = (
        f'Cliente marco pedido #{venta.id} como recibido.\n'
        f'Repartidor asignado: {empleado.nombre}\n'
        f'Confirma entrega final aqui: {delivered_url}'
    )

    result = _send_telegram(
        'sendMessage',
        {
            'chat_id': chat_id,
            'text': text,
            'disable_web_page_preview': True,
        },
    )
    return result is not None
