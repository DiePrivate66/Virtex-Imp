from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from pos.application.notifications import notify_customer_reported_received, notify_order_claimed
from pos.application.sales import send_sale_receipt_email_async
from pos.infrastructure.delivery import (
    read_delivery_claim_token,
    read_delivery_delivered_token,
    read_delivery_in_transit_token,
    read_delivery_quote_token,
)
from pos.infrastructure.tasks import queue_delivery_receipt_ticket
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


@dataclass(frozen=True)
class DeliveryInTransitSubmission:
    venta_id: int
    empleado_id: int
    empleado_nombre: str
    eta_minutos: int


@dataclass(frozen=True)
class DeliveryCompletionSubmission:
    venta_id: int
    empleado_id: int
    empleado_nombre: str


@dataclass(frozen=True)
class DeliveryDriverRegistration:
    empleado_id: int
    empleado_nombre: str
    pin: str


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


def _get_in_transit_token_payload(token: str) -> dict:
    try:
        return read_delivery_in_transit_token(token)
    except Exception as exc:
        raise DeliveryError('Token invalido o expirado') from exc


def _get_delivered_token_payload(token: str) -> dict:
    try:
        return read_delivery_delivered_token(token)
    except Exception as exc:
        raise DeliveryError('Token invalido o expirado') from exc


def _parse_eta_minutes(eta_minutos) -> int:
    try:
        eta = int(str(eta_minutos).strip())
    except (TypeError, ValueError) as exc:
        raise DeliveryError('Tiempo estimado invalido') from exc

    if eta <= 0 or eta > 180:
        raise DeliveryError('Tiempo estimado invalido')

    return eta


def _parse_delivery_pin(pin: str | None) -> str:
    normalized = (pin or '').strip()
    if not re.fullmatch(r'\d{4}', normalized):
        raise DeliveryError('El PIN debe tener exactamente 4 digitos.')
    return normalized


def register_delivery_driver(*, nombre: str, telefono: str, pin: str) -> DeliveryDriverRegistration:
    normalized_name = (nombre or '').strip()
    normalized_phone = (telefono or '').strip()
    normalized_pin = _parse_delivery_pin(pin)

    if not normalized_name:
        raise DeliveryError('El nombre es requerido para registrarte.')

    if not normalized_phone:
        raise DeliveryError('El telefono es requerido para registrarte.')

    if Empleado.objects.filter(pin=normalized_pin).exists():
        raise DeliveryError('Ese PIN ya esta en uso. Elige otro de 4 digitos.')

    driver = Empleado.objects.create(
        nombre=normalized_name,
        telefono=normalized_phone,
        pin=normalized_pin,
        rol='DELIVERY',
        activo=True,
    )

    return DeliveryDriverRegistration(
        empleado_id=driver.id,
        empleado_nombre=driver.nombre,
        pin=driver.pin,
    )


def register_delivery_and_claim_order(*, token: str, nombre: str, telefono: str, pin: str, precio) -> DeliveryClaimSubmission:
    payload = _get_claim_token_payload(token)

    venta = Venta.objects.filter(id=payload['venta_id']).first()
    if not venta:
        raise DeliveryError('Pedido no encontrado', status_code=404)

    if venta.repartidor_asignado is not None or venta.estado != 'PENDIENTE_COTIZACION':
        raise DeliveryError('Pedido ya tomado', status_code=409)

    registration = register_delivery_driver(nombre=nombre, telefono=telefono, pin=pin)
    return claim_delivery_order(token=token, pin=registration.pin, precio=precio)


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

    driver = Empleado.objects.filter(pin=_parse_delivery_pin(pin), rol='DELIVERY', activo=True).first()
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
    notify_order_claimed(venta, driver, precio_envio=precio_decimal)

    return DeliveryClaimSubmission(
        venta_id=venta.id,
        empleado_id=driver.id,
        empleado_nombre=driver.nombre,
        precio=str(precio_decimal),
    )


def mark_delivery_in_transit(*, token: str, pin: str, eta_minutos) -> DeliveryInTransitSubmission:
    payload = _get_in_transit_token_payload(token)
    eta = _parse_eta_minutes(eta_minutos)

    driver = Empleado.objects.filter(pin=(pin or '').strip(), rol='DELIVERY', activo=True).first()
    if not driver:
        raise DeliveryError('PIN invalido o no eres repartidor activo.')

    with transaction.atomic():
        try:
            venta = Venta.objects.select_for_update().get(id=payload['venta_id'])
        except Venta.DoesNotExist as exc:
            raise DeliveryError('Pedido no encontrado', status_code=404) from exc

        if venta.repartidor_asignado_id != payload['empleado_id'] or venta.repartidor_asignado_id != driver.id:
            raise DeliveryError('Este link no corresponde a tu pedido asignado.', status_code=403)

        if venta.estado in {'CANCELADO', 'LISTO', 'PENDIENTE_COTIZACION'}:
            raise DeliveryError('Este pedido ya no se puede marcar en camino.', status_code=409)

        venta.estado = 'EN_CAMINO'
        venta.tiempo_estimado_minutos = eta
        venta.salio_a_reparto_at = timezone.now()
        venta.save(update_fields=['estado', 'tiempo_estimado_minutos', 'salio_a_reparto_at'])

    return DeliveryInTransitSubmission(
        venta_id=venta.id,
        empleado_id=driver.id,
        empleado_nombre=driver.nombre,
        eta_minutos=eta,
    )


def mark_customer_received(*, pedido_id) -> Venta:
    try:
        venta = Venta.objects.select_related('repartidor_asignado', 'cliente').get(id=pedido_id, origen='WEB')
    except Venta.DoesNotExist as exc:
        raise DeliveryError('Pedido no encontrado', status_code=404) from exc

    if venta.tipo_pedido != 'DOMICILIO':
        raise DeliveryError('Solo los pedidos delivery pueden confirmarse desde aqui.', status_code=400)

    if venta.estado != 'EN_CAMINO':
        raise DeliveryError('El pedido aun no esta en camino.', status_code=409)

    if venta.repartidor_confirmo_entrega_at:
        raise DeliveryError('Este pedido ya fue confirmado como entregado.', status_code=409)

    if venta.cliente_reporto_recibido_at:
        return venta

    venta.cliente_reporto_recibido_at = timezone.now()
    venta.save(update_fields=['cliente_reporto_recibido_at'])

    if venta.repartidor_asignado:
        notify_customer_reported_received(venta, venta.repartidor_asignado)

    return venta


def confirm_delivery_completed(*, token: str, pin: str) -> DeliveryCompletionSubmission:
    payload = _get_delivered_token_payload(token)

    driver = Empleado.objects.filter(pin=(pin or '').strip(), rol='DELIVERY', activo=True).first()
    if not driver:
        raise DeliveryError('PIN invalido o no eres repartidor activo.')

    with transaction.atomic():
        try:
            venta = Venta.objects.select_for_update().select_related('cliente', 'repartidor_asignado').get(id=payload['venta_id'])
        except Venta.DoesNotExist as exc:
            raise DeliveryError('Pedido no encontrado', status_code=404) from exc

        if venta.repartidor_asignado_id != payload['empleado_id'] or venta.repartidor_asignado_id != driver.id:
            raise DeliveryError('Este link no corresponde a tu pedido asignado.', status_code=403)

        if venta.estado != 'EN_CAMINO':
            raise DeliveryError('Este pedido ya no esta en entrega.', status_code=409)

        if not venta.cliente_reporto_recibido_at:
            raise DeliveryError('El cliente aun no ha reportado el pedido como recibido.', status_code=409)

        if venta.repartidor_confirmo_entrega_at:
            return DeliveryCompletionSubmission(
                venta_id=venta.id,
                empleado_id=driver.id,
                empleado_nombre=driver.nombre,
            )

        venta.repartidor_confirmo_entrega_at = timezone.now()
        venta.estado = 'LISTO'
        venta.save(update_fields=['repartidor_confirmo_entrega_at', 'estado'])

    queue_delivery_receipt_ticket.delay(venta.id)
    recipient_email = (venta.email_cliente or '').strip()
    if not recipient_email and venta.cliente:
        recipient_email = (venta.cliente.email or '').strip()
    if recipient_email:
        send_sale_receipt_email_async(venta, recipient_email)

    return DeliveryCompletionSubmission(
        venta_id=venta.id,
        empleado_id=driver.id,
        empleado_nombre=driver.nombre,
    )
