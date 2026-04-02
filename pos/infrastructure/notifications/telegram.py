from __future__ import annotations

from datetime import timedelta
import json
import logging
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

import redis
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_telegram_redis_client = None


class TelegramCircuitBackendUnavailable(Exception):
    """Raised when the shared circuit-breaker backend is unavailable."""


def _telegram_api_url(method: str) -> str:
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    return f'https://api.telegram.org/bot{token}/{method}'


def _breaker_key(suffix: str) -> str:
    return f'bosco:telegram:circuit:{suffix}'


def _get_telegram_breaker_client():
    global _telegram_redis_client
    if _telegram_redis_client is None:
        timeout_seconds = float(getattr(settings, 'TELEGRAM_CIRCUIT_REDIS_TIMEOUT_SECONDS', 0.2))
        _telegram_redis_client = redis.Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
            decode_responses=True,
        )
    return _telegram_redis_client


def _handle_breaker_backend_unavailable(exc: Exception) -> None:
    logger.exception(
        'Redis no disponible para Telegram circuit breaker; se aplica fail-safe y se omiten envios externos.',
        exc_info=exc,
    )
    raise TelegramCircuitBackendUnavailable from exc


def _reset_telegram_circuit_breaker() -> None:
    keys = [_breaker_key('failure_count'), _breaker_key('opened_until')]
    try:
        _get_telegram_breaker_client().delete(*keys)
        return
    except Exception as exc:
        _handle_breaker_backend_unavailable(exc)


def _is_telegram_circuit_open(*, now=None) -> bool:
    current_time = now or timezone.now()
    try:
        opened_until = _get_telegram_breaker_client().get(_breaker_key('opened_until'))
    except Exception as exc:
        _handle_breaker_backend_unavailable(exc)
    if not opened_until:
        return False
    if current_time.timestamp() < float(opened_until):
        return True
    _reset_telegram_circuit_breaker()
    return False


def _record_telegram_failure(*, now=None) -> None:
    current_time = now or timezone.now()
    threshold = max(1, int(getattr(settings, 'TELEGRAM_CIRCUIT_FAILURE_THRESHOLD', 3)))
    cooldown_seconds = max(30, int(getattr(settings, 'TELEGRAM_CIRCUIT_OPEN_SECONDS', 180)))
    failure_key = _breaker_key('failure_count')
    try:
        client = _get_telegram_breaker_client()
        failures = int(client.incr(failure_key))
        if failures == 1:
            client.expire(failure_key, cooldown_seconds)
        if failures >= threshold:
            opened_until = current_time + timedelta(seconds=cooldown_seconds)
            client.setex(_breaker_key('opened_until'), cooldown_seconds, str(opened_until.timestamp()))
        return
    except Exception as exc:
        _handle_breaker_backend_unavailable(exc)


def _record_telegram_success() -> None:
    _reset_telegram_circuit_breaker()


def _send_telegram(method: str, payload: dict) -> Optional[dict]:
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    if not token:
        logger.debug('TELEGRAM_BOT_TOKEN not configured; skipping.')
        return None
    try:
        circuit_open = _is_telegram_circuit_open()
    except TelegramCircuitBackendUnavailable:
        logger.warning('Telegram circuit breaker backend unavailable; skipping outbound request.')
        return None

    if circuit_open:
        logger.warning('Telegram circuit breaker open; skipping outbound request.')
        return None

    req = urlrequest.Request(
        _telegram_api_url(method),
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    try:
        timeout_seconds = max(3, int(getattr(settings, 'TELEGRAM_API_TIMEOUT_SECONDS', 10)))
        with urlrequest.urlopen(req, timeout=timeout_seconds) as resp:
            response_payload = json.loads(resp.read().decode('utf-8'))
            try:
                if response_payload.get('ok'):
                    _record_telegram_success()
                else:
                    _record_telegram_failure()
            except TelegramCircuitBackendUnavailable:
                logger.warning('Telegram breaker backend unavailable while recording response state.')
            return response_payload
    except urlerror.HTTPError as exc:
        body = ''
        try:
            body = exc.read().decode('utf-8')
        except Exception:
            body = str(exc)
        try:
            _record_telegram_failure()
        except TelegramCircuitBackendUnavailable:
            logger.warning('Telegram breaker backend unavailable while recording HTTP failure.')
        logger.exception('Telegram API error: %s', body)
        return None
    except Exception:
        try:
            _record_telegram_failure()
        except TelegramCircuitBackendUnavailable:
            logger.warning('Telegram breaker backend unavailable while recording request failure.')
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


def notify_admin_exception_alert(payload: dict) -> bool:
    chat_id = getattr(settings, 'TELEGRAM_ADMIN_ALERT_CHAT_ID', '')
    if not chat_id:
        logger.debug('TELEGRAM_ADMIN_ALERT_CHAT_ID not configured; skipping admin alert.')
        return False

    venta_id = payload.get('venta_id', 'N/A')
    total = payload.get('total', '0.00')
    location_name = payload.get('location_name', 'Sucursal desconocida')
    payment_reference = payload.get('payment_reference') or 'SIN_REFERENCIA'
    payment_provider = payload.get('payment_provider') or 'DESCONOCIDO'
    action_url = payload.get('action_url', '')

    text = (
        'ALERTA CRITICA DE PAGO\n'
        f'Venta #{venta_id}\n'
        f'Sucursal: {location_name}\n'
        f'Monto: ${total}\n'
        f'Referencia: {payment_reference}\n'
        f'Pasarela: {payment_provider}\n'
        'Estado: pago recibido sobre venta anulada; revisar inmediatamente.'
    )
    if action_url:
        text += f'\nRevisar: {action_url}'

    result = _send_telegram(
        'sendMessage',
        {
            'chat_id': chat_id,
            'text': text,
            'disable_web_page_preview': True,
        },
    )
    return bool(result and result.get('ok'))
