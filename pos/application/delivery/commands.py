from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction

from pos.application.notifications import notify_order_claimed
from pos.infrastructure.delivery import read_delivery_claim_token, read_delivery_quote_token
from pos.models import Empleado, Venta
from pos.models import DeliveryQuote
from pos.infrastructure.tasks import set_quote_and_notify


class DeliveryError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class DeliveryQuoteSubmission:
    venta_id: int
    empleado_id: int
    precio: str


@dataclass(frozen=True)
class DeliveryClaimSubmission:
    venta_id: int
    empleado_id: int
    empleado_nombre: str
    precio: str


def _parse_delivery_price(precio) -> Decimal:
    try:
        precio_decimal = Decimal(str(precio)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DeliveryError('Precio invalido') from exc

    if precio_decimal <= 0:
        raise DeliveryError('Precio invalido')

    return precio_decimal


def _get_quote_token_payload(token: str) -> dict:
    try:
        return read_delivery_quote_token(token)
    except Exception as exc:
        raise DeliveryError('Token invalido o expirado') from exc


def _get_claim_token_payload(token: str) -> dict:
    try:
        return read_delivery_claim_token(token)
    except Exception as exc:
        raise DeliveryError('Token invalido o expirado') from exc


def submit_manual_delivery_quote(*, pedido_id, precio, user=None) -> DeliveryQuoteSubmission:
    try:
        venta = Venta.objects.get(id=pedido_id)
    except Venta.DoesNotExist as exc:
        raise DeliveryError('Pedido no encontrado', status_code=404) from exc

    empleado = None
    if user is not None and getattr(user, 'is_authenticated', False) and hasattr(user, 'empleado'):
        empleado = user.empleado
    if not empleado:
        empleado = Empleado.objects.filter(rol='DELIVERY', activo=True).first()
    if not empleado:
        raise DeliveryError('No hay delivery configurado')

    precio_decimal = _parse_delivery_price(precio)

    set_quote_and_notify.delay(venta.id, empleado.id, str(precio_decimal))
    return DeliveryQuoteSubmission(
        venta_id=venta.id,
        empleado_id=empleado.id,
        precio=str(precio_decimal),
    )


def submit_tokenized_delivery_quote(*, token: str, precio) -> DeliveryQuoteSubmission:
    payload = _get_quote_token_payload(token)

    venta = Venta.objects.filter(id=payload['venta_id']).first()
    if not venta:
        raise DeliveryError('Pedido no encontrado', status_code=404)

    if DeliveryQuote.objects.filter(
        venta_id=payload['venta_id'], empleado_delivery_id=payload['empleado_id']
    ).exists():
        raise DeliveryError('Esta cotizacion ya fue enviada', status_code=409)

    precio_decimal = _parse_delivery_price(precio)
    result = set_quote_and_notify.delay(payload['venta_id'], payload['empleado_id'], str(precio_decimal))

    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        task_status = (result.get() or {}).get('status')
        if task_status in {'duplicate_driver', 'ignored', 'late'}:
            raise DeliveryError('Cotizacion recibida fuera de ventana', status_code=409)

    return DeliveryQuoteSubmission(
        venta_id=payload['venta_id'],
        empleado_id=payload['empleado_id'],
        precio=str(precio_decimal),
    )


def claim_delivery_order(*, token: str, pin: str, precio) -> DeliveryClaimSubmission:
    payload = _get_claim_token_payload(token)
    precio_decimal = _parse_delivery_price(precio)

    driver = Empleado.objects.filter(pin=(pin or '').strip(), rol='DELIVERY', activo=True).first()
    if not driver:
        raise DeliveryError('PIN invalido o no eres repartidor activo.')

    with transaction.atomic():
        try:
            venta = Venta.objects.select_for_update().get(id=payload['venta_id'])
        except Venta.DoesNotExist as exc:
            raise DeliveryError('Pedido no encontrado', status_code=404) from exc

        if venta.repartidor_asignado is not None or venta.estado != 'PENDIENTE_COTIZACION':
            raise DeliveryError('Pedido ya tomado', status_code=409)

        venta.repartidor_asignado = driver
        venta.save(update_fields=['repartidor_asignado'])

    set_quote_and_notify.delay(venta.id, driver.id, str(precio_decimal))
    notify_order_claimed(venta, driver)

    return DeliveryClaimSubmission(
        venta_id=venta.id,
        empleado_id=driver.id,
        empleado_nombre=driver.nombre,
        precio=str(precio_decimal),
    )
