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


def notify_delivery_group(venta) -> bool:
    """Send order details + GPS location to the Telegram delivery group."""
    chat_id = getattr(settings, 'TELEGRAM_DELIVERY_GROUP_ID', '')
    if not chat_id:
        logger.debug('TELEGRAM_DELIVERY_GROUP_ID not configured; skipping.')
        return False

    map_url = ''
    if venta.ubicacion_lat is not None and venta.ubicacion_lng is not None:
        map_url = f'https://www.google.com/maps?q={venta.ubicacion_lat},{venta.ubicacion_lng}'

    items_text = ''
    for detalle in venta.detalles.select_related('producto').all():
        nota = f' ({detalle.nota})' if detalle.nota else ''
        items_text += f'  • {detalle.cantidad}x {detalle.producto.nombre}{nota}\n'

    text = (
        f'🔔 *Nuevo Pedido #{venta.id}*\n'
        f'👤 {venta.cliente_nombre}\n'
        f'📞 {venta.telefono_cliente or "No proporcionado"}\n'
        f'📍 {venta.direccion_envio or "Sin direccion"}\n'
        f'🗺 {map_url or "Ubicacion no compartida"}\n\n'
        f'🛒 *Detalle:*\n{items_text or "  (sin items)"}\n'
        f'💰 *Total:* ${venta.total:.2f}\n'
        f'💳 Pago: {venta.get_metodo_pago_display()}'
    )

    result = _send_telegram('sendMessage', {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown',
    })

    if result and venta.ubicacion_lat is not None and venta.ubicacion_lng is not None:
        _send_telegram('sendLocation', {
            'chat_id': chat_id,
            'latitude': venta.ubicacion_lat,
            'longitude': venta.ubicacion_lng,
        })

    return result is not None
