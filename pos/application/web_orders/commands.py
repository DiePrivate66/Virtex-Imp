from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from pos.application.context import get_default_catalog_organization
from pos.domain.shared import normalize_phone_to_e164
from pos.domain.shared.sale_invariants import build_sale_payment_fields, build_sale_scope_fields
from pos.domain.web_orders import (
    QUOTE_EDITABLE_STATUSES,
    STATUS_CANCELLED,
    STATUS_IN_TRANSIT,
    STATUS_KITCHEN,
    STATUS_PENDING_QUOTE,
    STATUS_READY,
    can_transition,
)
from pos.infrastructure.tasks import process_delivery_quote_timeout, send_delivery_quote_requests
from pos.models import CajaTurno, Cliente, DetalleVenta, Location, Producto, V2_TO_LEGACY_PAYMENT_STATUS, Venta, WhatsAppConversation, compute_operating_day


class WebOrderError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class WebOrderTransitionError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def get_web_order(pedido_id) -> Venta:
    if not pedido_id:
        raise WebOrderTransitionError('Pedido no encontrado', status_code=404)

    try:
        return Venta.objects.get(id=pedido_id, origen='WEB')
    except Venta.DoesNotExist as exc:
        raise WebOrderTransitionError('Pedido no encontrado', status_code=404) from exc


def require_transition(venta: Venta, target_state: str) -> None:
    if not can_transition(venta.estado, target_state):
        raise WebOrderTransitionError(
            f'No se puede pasar de {venta.get_estado_display()} a {target_state.lower().replace("_", " ")}',
            status_code=400,
        )


def set_delivery_cost(pedido_id, costo_envio) -> Venta:
    venta = get_web_order(pedido_id)
    try:
        costo_decimal = Decimal(str(costo_envio))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise WebOrderTransitionError('Costo de envio invalido', status_code=400) from exc

    if costo_decimal < 0:
        raise WebOrderTransitionError('Costo de envio invalido', status_code=400)
    if venta.estado not in QUOTE_EDITABLE_STATUSES:
        raise WebOrderTransitionError('No se puede actualizar el costo de envio en este estado', status_code=400)

    venta.costo_envio = costo_decimal
    venta.save(update_fields=['costo_envio'])
    return venta


def accept_web_order(pedido_id) -> Venta:
    venta = get_web_order(pedido_id)
    require_transition(venta, STATUS_KITCHEN)
    if venta.estado != STATUS_KITCHEN:
        venta.estado = STATUS_KITCHEN
        venta.save(update_fields=['estado'])
    return venta


def mark_order_in_transit(pedido_id) -> Venta:
    venta = get_web_order(pedido_id)
    require_transition(venta, STATUS_IN_TRANSIT)
    if venta.estado != STATUS_IN_TRANSIT:
        venta.estado = STATUS_IN_TRANSIT
        venta.save(update_fields=['estado'])
    return venta


def mark_order_ready(pedido_id) -> Venta:
    venta = get_web_order(pedido_id)
    require_transition(venta, STATUS_READY)
    if venta.estado != STATUS_READY:
        venta.estado = STATUS_READY
        venta.save(update_fields=['estado'])
    return venta


def cancel_web_order(pedido_id) -> Venta:
    venta = get_web_order(pedido_id)
    require_transition(venta, STATUS_CANCELLED)
    if venta.estado != STATUS_CANCELLED:
        venta.estado = STATUS_CANCELLED
        venta.save(update_fields=['estado'])
    return venta


def create_web_order(data: dict, comprobante=None) -> Venta:
    cart = data.get('carrito', [])
    if not cart:
        raise WebOrderError('El carrito esta vacio', status_code=400)

    organization = get_default_catalog_organization()
    total = Decimal('0.00')
    validated_items = []
    for item in cart:
        try:
            product = Producto.objects.get(id=item['id'], organization=organization, activo=True)
        except (KeyError, Producto.DoesNotExist) as exc:
            raise WebOrderError('Producto no encontrado o no disponible', status_code=400) from exc
        quantity = int(item.get('cantidad', 1))
        subtotal = product.precio * quantity
        total += subtotal
        validated_items.append(
            {
                'producto': product,
                'cantidad': quantity,
                'precio_unitario': product.precio,
                'nombre_display': item.get('nombre', product.nombre),
                'nota': item.get('nota', ''),
            }
        )

    customer = _resolve_customer(data, organization=organization)
    cash_register = CajaTurno.objects.filter(fecha_cierre__isnull=True).first()
    sale_scope = build_sale_scope_fields(
        turno=cash_register,
        location=cash_register.location if cash_register and cash_register.location_id else None,
        organization=cash_register.organization if cash_register and cash_register.organization_id else None,
        timestamp=timezone.now(),
        default_location_getter=Location.get_or_create_default,
        compute_operating_day_fn=compute_operating_day,
    )
    order_type = data.get('tipo_pedido', 'DOMICILIO')
    initial_status = STATUS_PENDING_QUOTE if order_type == 'DOMICILIO' else 'PENDIENTE'

    lat = data.get('ubicacion_lat')
    lng = data.get('ubicacion_lng')
    raw_phone = data.get('telefono', '')

    with transaction.atomic():
        sale = Venta.objects.create(
            cliente=customer,
            cliente_nombre=data.get('nombre', 'CONSUMIDOR FINAL'),
            telefono_cliente=raw_phone,
            telefono_cliente_e164=normalize_phone_to_e164(raw_phone),
            email_cliente=(data.get('email') or '').strip(),
            direccion_envio=data.get('direccion', ''),
            ubicacion_lat=float(lat) if lat else None,
            ubicacion_lng=float(lng) if lng else None,
            tipo_pedido=order_type,
            total=total,
            monto_recibido=total if data.get('metodo_pago') == 'TRANSFERENCIA' else Decimal('0.00'),
            origen='WEB',
            estado=initial_status,
            turno=cash_register,
            comprobante_foto=comprobante,
            confirmacion_cliente='PENDIENTE',
            delivery_quote_deadline_at=(
                timezone.now() + timedelta(seconds=settings.DELIVERY_QUOTE_TIMEOUT_SECONDS)
                if order_type == 'DOMICILIO'
                else None
            ),
            **sale_scope,
            **build_sale_payment_fields(
                payment_status=Venta.PaymentStatus.PAID,
                metodo_pago=data.get('metodo_pago', 'EFECTIVO'),
                referencia_pago=data.get('referencia_pago', ''),
                valid_payment_statuses=Venta.PaymentStatus.values,
                payment_methods=Venta.METODOS,
                legacy_to_v2_map={},
                v2_to_legacy_map=V2_TO_LEGACY_PAYMENT_STATUS,
                default_payment_status=Venta.PaymentStatus.PAID,
            ),
        )

        for item_data in validated_items:
            product = item_data['producto']
            display_name = item_data['nombre_display']
            user_note = item_data['nota']

            final_note = ''
            if display_name != product.nombre:
                final_note = display_name.replace(product.nombre, '').strip()
            if user_note:
                final_note = f'{final_note} | {user_note}' if final_note else user_note

            DetalleVenta.objects.create(
                venta=sale,
                producto=product,
                cantidad=item_data['cantidad'],
                precio_unitario=item_data['precio_unitario'],
                nota=final_note.strip(),
            )

        _link_whatsapp_conversation(sale)

    if order_type == 'DOMICILIO':
        send_delivery_quote_requests.delay(sale.id)
        process_delivery_quote_timeout.apply_async(
            args=[sale.id], countdown=settings.DELIVERY_QUOTE_TIMEOUT_SECONDS
        )

    return sale


def _resolve_customer(data: dict, *, organization):
    cedula = data.get('cedula', '').strip()
    if not cedula:
        return None

    customer, _ = Cliente.objects.get_or_create(
        organization=organization,
        cedula_ruc=cedula,
        defaults={
            'nombre': data.get('nombre', 'CONSUMIDOR FINAL'),
            'telefono': data.get('telefono', ''),
            'direccion': data.get('direccion', ''),
            'email': data.get('email', '').strip(),
        },
    )
    updated_fields = []
    nombre = data.get('nombre', 'CONSUMIDOR FINAL')
    telefono = data.get('telefono', '')
    direccion = data.get('direccion', '')
    email = data.get('email', '').strip()
    if nombre and customer.nombre != nombre:
        customer.nombre = nombre
        updated_fields.append('nombre')
    if telefono and customer.telefono != telefono:
        customer.telefono = telefono
        updated_fields.append('telefono')
    if direccion and customer.direccion != direccion:
        customer.direccion = direccion
        updated_fields.append('direccion')
    if email and customer.email != email:
        customer.email = email
        updated_fields.append('email')
    if updated_fields:
        customer.save(update_fields=updated_fields)
    return customer


def _link_whatsapp_conversation(sale: Venta) -> None:
    if not sale.telefono_cliente_e164:
        return

    conversation, _ = WhatsAppConversation.objects.get_or_create(telefono_e164=sale.telefono_cliente_e164)
    conversation.venta = sale
    conversation.save(update_fields=['venta'])
