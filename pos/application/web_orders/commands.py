from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.urls import reverse

from pos.application.context import get_default_catalog_organization
from pos.application.sales.offline_capture import capture_paid_sale_to_offline_journal
from pos.domain.shared import build_sale_temporal_fields, normalize_phone_to_e164
from pos.domain.shared.sale_invariants import (
    build_sale_detail_fields,
    build_sale_payment_fields,
    build_sale_scope_fields,
)
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
from pos.infrastructure.payments import PayPhoneError, confirm_payphone_transaction, payphone_web_checkout_enabled, prepare_payphone_checkout
from pos.models import (
    CajaTurno,
    Cliente,
    DetalleVenta,
    Location,
    PendingOfflineOrphanEvent,
    Producto,
    V2_TO_LEGACY_PAYMENT_STATUS,
    Venta,
    WhatsAppConversation,
    compute_operating_day,
)


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


PAYPHONE_PROVIDER = 'PAYPHONE'
PAYPHONE_SUPPORTED_ORDER_TYPES = {'LLEVAR'}


def _extract_payment_reference(data: dict) -> str:
    return (data.get('payment_reference') or data.get('referencia_pago') or '').strip()


def _extract_payment_method(data: dict) -> str:
    return str(data.get('metodo_pago', 'EFECTIVO') or 'EFECTIVO').strip().upper()


def _build_web_order_client_transaction_id(data: dict) -> str:
    candidate = str(data.get('client_transaction_id') or '').strip()
    if candidate:
        return candidate[:64]
    return f'WEBPAY-{uuid4().hex[:24]}'


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
    _require_paid_web_order(venta)
    require_transition(venta, STATUS_KITCHEN)
    if venta.estado != STATUS_KITCHEN:
        venta.estado = STATUS_KITCHEN
        venta.save(update_fields=['estado'])
    return venta


def mark_order_in_transit(pedido_id) -> Venta:
    venta = get_web_order(pedido_id)
    _require_paid_web_order(venta)
    require_transition(venta, STATUS_IN_TRANSIT)
    if venta.estado != STATUS_IN_TRANSIT:
        venta.estado = STATUS_IN_TRANSIT
        venta.save(update_fields=['estado'])
    return venta


def mark_order_ready(pedido_id) -> Venta:
    venta = get_web_order(pedido_id)
    _require_paid_web_order(venta)
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
                **build_sale_detail_fields(
                    product_name=product.nombre,
                    quantity=quantity,
                    unit_price=product.precio,
                    gross_unit_price=product.precio,
                    pricing_rule_snapshot={'source': 'product.precio', 'product_price': f'{product.precio:.2f}'},
                    tax_rule_snapshot={},
                    discount_rule_snapshot={},
                    display_name=item.get('nombre', product.nombre),
                    user_note=item.get('nota', ''),
                ),
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
    payment_method = _extract_payment_method(data)
    order_type = data.get('tipo_pedido', 'DOMICILIO')
    if payment_method == PAYPHONE_PROVIDER and order_type not in PAYPHONE_SUPPORTED_ORDER_TYPES:
        raise WebOrderError('PayPhone solo esta disponible para pedidos para llevar', status_code=400)
    if payment_method == PAYPHONE_PROVIDER and not payphone_web_checkout_enabled():
        raise WebOrderError('PayPhone no esta disponible en este momento', status_code=503)
    initial_status = STATUS_PENDING_QUOTE if order_type == 'DOMICILIO' else 'PENDIENTE'
    initial_payment_status = Venta.PaymentStatus.PENDING if payment_method == PAYPHONE_PROVIDER else Venta.PaymentStatus.PAID
    client_transaction_id = _build_web_order_client_transaction_id(data)

    lat = data.get('ubicacion_lat')
    lng = data.get('ubicacion_lng')
    raw_phone = data.get('telefono', '')
    received_at = timezone.now()
    location_for_timeline = sale_scope.get('location')
    temporal_fields = build_sale_temporal_fields(
        received_at=received_at,
        queue_session_id=data.get('queue_session_id', ''),
        session_seq_no=data.get('session_seq_no'),
        client_created_at_raw=data.get('client_created_at_raw'),
        client_monotonic_ms=data.get('client_monotonic_ms'),
        timezone_name=location_for_timeline.timezone if location_for_timeline else None,
        chronology_threshold_minutes=int(
            getattr(settings, 'SALE_CHRONOLOGY_ESTIMATED_THRESHOLD_MINUTES', 15)
        ),
    )

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
            monto_recibido=total if payment_method == 'TRANSFERENCIA' else Decimal('0.00'),
            origen='WEB',
            estado=initial_status,
            turno=cash_register,
            comprobante_foto=comprobante,
            confirmacion_cliente='PENDIENTE',
            client_transaction_id=client_transaction_id,
            payment_provider=PAYPHONE_PROVIDER if payment_method == PAYPHONE_PROVIDER else '',
            delivery_quote_deadline_at=(
                timezone.now() + timedelta(seconds=settings.DELIVERY_QUOTE_TIMEOUT_SECONDS)
                if order_type == 'DOMICILIO'
                else None
            ),
            **sale_scope,
            **temporal_fields,
            **build_sale_payment_fields(
                payment_status=initial_payment_status,
                metodo_pago=payment_method,
                payment_reference=_extract_payment_reference(data),
                valid_payment_statuses=Venta.PaymentStatus.values,
                payment_methods=Venta.METODOS,
                v2_to_legacy_map=V2_TO_LEGACY_PAYMENT_STATUS,
                default_payment_status=initial_payment_status,
            ),
        )

        for item_data in validated_items:
            DetalleVenta.objects.create(
                venta=sale,
                producto=item_data['producto'],
                cantidad=item_data['cantidad'],
                precio_unitario=item_data['precio_unitario'],
                precio_bruto_unitario=item_data['precio_bruto_unitario'],
                descuento_monto=item_data['descuento_monto'],
                impuesto_monto=item_data['impuesto_monto'],
                subtotal_neto=item_data['subtotal_neto'],
                pricing_rule_snapshot=item_data['pricing_rule_snapshot'],
                tax_rule_snapshot=item_data['tax_rule_snapshot'],
                discount_rule_snapshot=item_data['discount_rule_snapshot'],
                nota=item_data['nota'],
            )

        _link_whatsapp_conversation(sale)
        if sale.payment_status == Venta.PaymentStatus.PAID:
            transaction.on_commit(
                lambda venta_id=sale.id: capture_paid_sale_to_offline_journal(
                    venta_id=venta_id,
                    capture_event_type='sale.web_order_created',
                    capture_source='server_django_web_orders',
                )
            )

    if order_type == 'DOMICILIO':
        send_delivery_quote_requests.delay(sale.id)
        process_delivery_quote_timeout.apply_async(
            args=[sale.id], countdown=settings.DELIVERY_QUOTE_TIMEOUT_SECONDS
        )

    return sale


def prepare_payphone_checkout_for_web_order(venta: Venta, *, remote_ip: str = '') -> dict:
    if venta.origen != 'WEB':
        raise WebOrderError('Solo los pedidos web pueden preparar PayPhone', status_code=400)
    if venta.metodo_pago != PAYPHONE_PROVIDER:
        raise WebOrderError('El pedido no usa PayPhone', status_code=400)
    if venta.tipo_pedido not in PAYPHONE_SUPPORTED_ORDER_TYPES:
        raise WebOrderError('PayPhone solo esta disponible para pedidos para llevar', status_code=400)
    if venta.payment_status == Venta.PaymentStatus.PAID:
        raise WebOrderError('El pedido ya fue pagado', status_code=409)

    response_url = _build_payphone_return_url(venta)
    cancellation_url = _build_payphone_cancel_url(venta)
    line_items = [
        {
            'productName': detalle.producto.nombre,
            'unitPrice': _decimal_to_cents(detalle.precio_unitario),
            'quantity': detalle.cantidad,
            'totalAmount': _decimal_to_cents(detalle.subtotal),
            'taxAmount': 0,
            'productSKU': str(detalle.producto_id),
            'productDescription': detalle.nota or detalle.producto.nombre,
        }
        for detalle in venta.detalles.select_related('producto').all()
    ]
    payload = {
        'amount': _decimal_to_cents(venta.total),
        'amountWithoutTax': _decimal_to_cents(venta.total),
        'amountWithTax': 0,
        'tax': 0,
        'service': 0,
        'tip': 0,
        'clientTransactionId': venta.client_transaction_id,
        'reference': _build_payphone_sale_reference(venta),
        'storeId': str(getattr(settings, 'PAYPHONE_STORE_ID', '')),
        'currency': 'USD',
        'responseUrl': response_url,
        'cancellationUrl': cancellation_url,
        'timeZone': -5,
        'phoneNumber': venta.telefono_cliente_e164 or normalize_phone_to_e164(venta.telefono_cliente),
        'email': venta.email_cliente or None,
        'documentId': venta.cliente.cedula_ruc if venta.cliente_id and venta.cliente and venta.cliente.cedula_ruc else None,
        'optionalParameter': f'WEB_ORDER:{venta.id}',
        'order': {
            'lineItems': line_items,
        },
    }
    if remote_ip:
        payload['order']['billTo'] = {
            'billToId': venta.id,
            'customerId': str(venta.cliente_id or venta.id),
            'firstName': venta.cliente_nombre[:50],
            'phoneNumber': venta.telefono_cliente_e164 or normalize_phone_to_e164(venta.telefono_cliente),
            'email': venta.email_cliente or '',
            'ipAddress': remote_ip,
            'country': 'EC',
        }

    try:
        prepare_response = prepare_payphone_checkout(_compact_dict(payload))
    except PayPhoneError as exc:
        raise WebOrderError('No se pudo iniciar el cobro con PayPhone', status_code=502) from exc
    checkout_url = prepare_response.get('payWithCard') or prepare_response.get('payWithPayPhone')
    if not prepare_response.get('paymentId') or not checkout_url:
        raise WebOrderError('PayPhone no devolvio un enlace de cobro valido', status_code=502)
    return {
        'payment_id': prepare_response.get('paymentId'),
        'payphone_checkout_url': checkout_url,
        'payphone_card_url': prepare_response.get('payWithCard', ''),
        'payphone_app_url': prepare_response.get('payWithPayPhone', ''),
        'payphone_checkout_expires_in_seconds': 600,
    }


def confirm_payphone_web_order(*, venta: Venta, payphone_id: int | str, client_transaction_id: str) -> dict:
    if venta.origen != 'WEB':
        raise WebOrderError('Solo los pedidos web pueden confirmar pagos PayPhone', status_code=400)
    if venta.metodo_pago != PAYPHONE_PROVIDER:
        raise WebOrderError('El pedido no usa PayPhone', status_code=400)

    try:
        confirm_response = confirm_payphone_transaction(
            payphone_id=payphone_id,
            client_transaction_id=client_transaction_id,
        )
    except PayPhoneError as exc:
        raise WebOrderError('No se pudo confirmar el pago PayPhone', status_code=502) from exc
    payphone_client_transaction_id = str(confirm_response.get('clientTransactionId') or '')
    if payphone_client_transaction_id and payphone_client_transaction_id != venta.client_transaction_id:
        raise WebOrderError('La confirmacion PayPhone no coincide con el pedido', status_code=409)

    transaction_status = str(confirm_response.get('transactionStatus') or '').strip().lower()
    status_code = int(confirm_response.get('statusCode') or 0)
    if status_code == 3 and transaction_status == 'approved':
        venta = _mark_web_order_payment_paid(venta=venta, confirm_response=confirm_response)
        return {
            'status': 'paid',
            'venta': venta,
            'confirm_response': confirm_response,
        }

    venta = _mark_web_order_payment_failed(
        venta=venta,
        reason=confirm_response.get('message') or 'El pago PayPhone no fue aprobado',
        failure_status=Venta.PaymentStatus.VOIDED if status_code == 2 else Venta.PaymentStatus.FAILED,
        confirm_response=confirm_response,
    )
    return {
        'status': 'failed',
        'venta': venta,
        'confirm_response': confirm_response,
    }


def cancel_payphone_web_order(venta: Venta, *, reason: str = 'Pago cancelado desde PayPhone') -> Venta:
    if venta.metodo_pago != PAYPHONE_PROVIDER:
        raise WebOrderError('El pedido no usa PayPhone', status_code=400)
    if venta.payment_status == Venta.PaymentStatus.PAID:
        return venta
    return _mark_web_order_payment_failed(
        venta=venta,
        reason=reason,
        failure_status=Venta.PaymentStatus.VOIDED,
        confirm_response={'message': reason, 'transactionStatus': 'Canceled', 'statusCode': 2},
    )


def process_payphone_notification(payload: dict) -> tuple[dict, int]:
    try:
        client_transaction_id = str(payload.get('ClientTransactionId') or '').strip()
        if not client_transaction_id:
            return {'Response': False, 'ErrorCode': '444'}, 400
        venta = Venta.objects.filter(origen='WEB', metodo_pago=PAYPHONE_PROVIDER, client_transaction_id=client_transaction_id).first()
        if not venta:
            PendingOfflineOrphanEvent.objects.get_or_create(
                event_type='payphone_notification',
                client_transaction_id=client_transaction_id[:64],
                payment_reference=str(payload.get('TransactionId') or payload.get('Reference') or '')[:80],
                defaults={
                    'payment_provider': PAYPHONE_PROVIDER,
                    'payload_json': payload,
                    'correlation_id': client_transaction_id[:64],
                },
            )
            return {'Response': True, 'ErrorCode': '000'}, 200

        status_code = int(payload.get('StatusCode') or 0)
        transaction_status = str(payload.get('TransactionStatus') or '').strip().lower()
        if status_code == 3 and transaction_status == 'approved':
            _mark_web_order_payment_paid(venta=venta, confirm_response=_normalize_payphone_notification_payload(payload))
        elif venta.payment_status != Venta.PaymentStatus.PAID:
            _mark_web_order_payment_failed(
                venta=venta,
                reason=payload.get('Message') or payload.get('TransactionStatus') or 'El pago PayPhone no fue aprobado',
                failure_status=Venta.PaymentStatus.VOIDED if status_code == 2 else Venta.PaymentStatus.FAILED,
                confirm_response=_normalize_payphone_notification_payload(payload),
            )
        return {'Response': True, 'ErrorCode': '000'}, 200
    except Exception:
        return {'Response': False, 'ErrorCode': '222'}, 500


def _require_paid_web_order(venta: Venta) -> None:
    if venta.payment_status != Venta.PaymentStatus.PAID:
        raise WebOrderTransitionError('No se puede procesar el pedido hasta confirmar el pago', status_code=409)


def _mark_web_order_payment_paid(*, venta: Venta, confirm_response: dict) -> Venta:
    if venta.payment_status == Venta.PaymentStatus.PAID:
        return venta

    payment_reference = _resolve_payphone_payment_reference(confirm_response)
    with transaction.atomic():
        venta = Venta.objects.select_for_update().get(id=venta.id)
        if venta.payment_status == Venta.PaymentStatus.PAID:
            return venta
        venta.payment_status = Venta.PaymentStatus.PAID
        venta.payment_provider = PAYPHONE_PROVIDER
        venta.payment_checked_at = timezone.now()
        venta.payment_failure_reason = ''
        venta.payment_reference = payment_reference
        venta.referencia_pago = payment_reference[:40]
        venta.tarjeta_tipo = str(confirm_response.get('cardType') or venta.tarjeta_tipo or '')[:12].upper()
        venta.tarjeta_marca = str(confirm_response.get('cardBrand') or venta.tarjeta_marca or '')[:20]
        venta.save(
            update_fields=[
                'payment_status',
                'payment_provider',
                'payment_checked_at',
                'payment_failure_reason',
                'payment_reference',
                'referencia_pago',
                'tarjeta_tipo',
                'tarjeta_marca',
            ]
        )
        transaction.on_commit(
            lambda venta_id=venta.id: capture_paid_sale_to_offline_journal(
                venta_id=venta_id,
                capture_event_type='sale.web_order_payphone_confirmed',
                capture_source='server_django_payphone',
            )
        )
    return venta


def _mark_web_order_payment_failed(
    *,
    venta: Venta,
    reason: str,
    failure_status: str,
    confirm_response: dict,
) -> Venta:
    if venta.payment_status == Venta.PaymentStatus.PAID:
        return venta
    venta.payment_status = failure_status
    venta.payment_provider = PAYPHONE_PROVIDER
    venta.payment_checked_at = timezone.now()
    venta.payment_failure_reason = str(reason or '')[:255]
    payment_reference = _resolve_payphone_payment_reference(confirm_response)
    if payment_reference:
        venta.payment_reference = payment_reference
        venta.referencia_pago = payment_reference[:40]
    venta.estado = STATUS_CANCELLED
    venta.save(
        update_fields=[
            'payment_status',
            'payment_provider',
            'payment_checked_at',
            'payment_failure_reason',
            'payment_reference',
            'referencia_pago',
            'estado',
        ]
    )
    return venta


def _build_payphone_return_url(venta: Venta) -> str:
    return f"{settings.PUBLIC_BACKEND_URL.rstrip('/')}{reverse('pedido_api_payphone_return')}?pedido_id={venta.id}"


def _build_payphone_cancel_url(venta: Venta) -> str:
    return f"{settings.PUBLIC_BACKEND_URL.rstrip('/')}{reverse('pedido_api_payphone_cancel')}?pedido_id={venta.id}"


def _build_payphone_sale_reference(venta: Venta) -> str:
    return f'Pedido web #{venta.id}'


def _decimal_to_cents(value: Decimal) -> int:
    return int((Decimal(str(value)).quantize(Decimal('0.01')) * 100).to_integral_value())


def _compact_dict(payload: dict) -> dict:
    compacted = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            nested = _compact_dict(value)
            if nested:
                compacted[key] = nested
            continue
        if value in (None, '', [], {}):
            continue
        compacted[key] = value
    return compacted


def _resolve_payphone_payment_reference(confirm_response: dict) -> str:
    transaction_id = confirm_response.get('transactionId')
    if transaction_id:
        return f'PAYPHONE-{transaction_id}'
    authorization_code = str(confirm_response.get('authorizationCode') or '').strip()
    if authorization_code:
        return authorization_code
    return str(confirm_response.get('clientTransactionId') or '').strip()[:80]


def _normalize_payphone_notification_payload(payload: dict) -> dict:
    return {
        'transactionId': payload.get('TransactionId'),
        'authorizationCode': payload.get('AuthorizationCode'),
        'clientTransactionId': payload.get('ClientTransactionId'),
        'statusCode': payload.get('StatusCode'),
        'transactionStatus': payload.get('TransactionStatus'),
        'cardType': payload.get('CardType'),
        'cardBrand': payload.get('CardBrand'),
        'reference': payload.get('Reference'),
        'amount': payload.get('Amount'),
    }


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
