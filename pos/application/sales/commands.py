from __future__ import annotations

import re
import threading
from decimal import Decimal

from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

from pos.application.cash_register import find_customer_by_identity_document, get_open_cash_register_for_user
from pos.models import Cliente, DetalleVenta, Producto, Venta


class PosSaleError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def register_sale(user, data: dict):
    cedula_input = (data.get('cliente_cedula') or '').strip()
    consumidor_final = bool(data.get('consumidor_final'))
    metodo_pago = (data.get('metodo_pago') or '').upper().strip()
    total_venta = Decimal(str(data.get('total') or 0)).quantize(Decimal('0.01'))
    referencia_pago = _normalize_reference(data.get('referencia_pago'))
    tarjeta_tipo = _normalize_simple_text(data.get('tarjeta_tipo'), 12)
    tarjeta_marca = _normalize_simple_text(data.get('tarjeta_marca'), 20)

    turno_activo = get_open_cash_register_for_user(user)
    if not turno_activo:
        raise PosSaleError('No hay caja activa para registrar ventas', status_code=400)

    if metodo_pago not in {'EFECTIVO', 'TRANSFERENCIA', 'TARJETA'}:
        raise PosSaleError('Metodo de pago invalido', status_code=400)

    if total_venta <= 0:
        raise PosSaleError('El total de la venta debe ser mayor a 0', status_code=400)

    if metodo_pago == 'TARJETA':
        _validate_card_payment(total_venta, referencia_pago, tarjeta_tipo)

    cliente = _resolve_customer(data, consumidor_final, cedula_input)

    venta = Venta.objects.create(
        cliente_nombre=data.get('cliente_nombre', 'CONSUMIDOR FINAL'),
        cliente=cliente,
        metodo_pago=metodo_pago,
        referencia_pago=referencia_pago,
        tarjeta_tipo=tarjeta_tipo,
        tarjeta_marca=tarjeta_marca,
        estado_pago='APROBADO',
        total=total_venta,
        origen='POS',
        estado='COCINA',
        tipo_pedido=data.get('tipo_pedido', 'SERVIR'),
        monto_recibido=data.get('monto_recibido', 0),
        turno=turno_activo,
    )

    for item in data.get('carrito', []):
        producto = Producto.objects.get(id=item['id'])
        DetalleVenta.objects.create(
            venta=venta,
            producto=producto,
            cantidad=item['cantidad'],
            precio_unitario=item['precio'],
            nota=_build_sale_note(producto.nombre, item.get('nombre', producto.nombre), item.get('nota', '')),
        )

    if cliente and cliente.email:
        send_sale_receipt_email_async(venta, cliente.email)

    return venta


def _resolve_customer(data: dict, consumidor_final: bool, cedula_input: str):
    if consumidor_final:
        return None

    if data.get('cliente_id'):
        cliente = Cliente.objects.filter(id=data.get('cliente_id')).first()
        if not cliente:
            raise PosSaleError('Cliente no encontrado', status_code=400)
        if not _is_valid_identity(cliente.cedula_ruc):
            raise PosSaleError('C.I/RUC invalido (10 o 13 digitos)', status_code=400)
        return cliente

    if not _is_valid_identity(cedula_input):
        raise PosSaleError('C.I/RUC invalido (10 o 13 digitos)', status_code=400)

    return find_customer_by_identity_document(cedula_input)


def _validate_card_payment(total_venta: Decimal, referencia_pago: str, tarjeta_tipo: str):
    if len(referencia_pago) < 6:
        raise PosSaleError('Referencia de tarjeta obligatoria (minimo 6 caracteres)', status_code=400)
    if not tarjeta_tipo:
        raise PosSaleError('Tipo de tarjeta obligatorio (credito o debito)', status_code=400)
    if tarjeta_tipo not in {'CREDITO', 'DEBITO'}:
        raise PosSaleError('Tipo de tarjeta invalido', status_code=400)

    hoy = timezone.localtime().date()
    existe_tarjeta = (
        Venta.objects.filter(
            origen='POS',
            metodo_pago='TARJETA',
            referencia_pago=referencia_pago,
            total=total_venta,
            fecha__date=hoy,
        )
        .exclude(estado='CANCELADO')
        .exclude(estado_pago='ANULADO')
        .first()
    )
    if existe_tarjeta:
        raise PosSaleError(
            f'Pago con tarjeta duplicado detectado (venta #{existe_tarjeta.id})',
            status_code=400,
        )


def _build_sale_note(product_name: str, display_name: str, user_note: str) -> str:
    note = ''
    if display_name != product_name:
        note = display_name.replace(product_name, '').strip()
    if user_note:
        note = f'{note} | {user_note}' if note else user_note
    return note.strip()


def send_sale_receipt_email_async(venta: Venta, recipient_email: str):
    html_email = render_to_string('pos/email/factura_email.html', {'venta': venta})

    def send_async():
        send_mail(
            subject=f'RAMON by Bosco - Comprobante de Venta #{venta.id}',
            message=f'Adjunto su comprobante de venta #{venta.id} por ${venta.total}',
            from_email=None,
            recipient_list=[recipient_email],
            html_message=html_email,
            fail_silently=True,
        )

    threading.Thread(target=send_async, daemon=True).start()


def _is_valid_identity(value: str) -> bool:
    return bool(value) and value.isdigit() and len(value) in (10, 13)


def _normalize_reference(value: str) -> str:
    ref = (value or '').upper().strip()
    ref = re.sub(r'\s+', '', ref)
    ref = re.sub(r'[^A-Z0-9\\-_/]', '', ref)
    return ref[:40]


def _normalize_simple_text(value: str, max_len: int) -> str:
    text = (value or '').upper().strip()
    text = re.sub(r'[^A-Z0-9 ]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text[:max_len]
