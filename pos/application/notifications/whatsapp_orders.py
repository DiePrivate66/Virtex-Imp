from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from pos.models import Venta

from .whatsapp import send_whatsapp_text

MAX_RECEIPT_ITEMS = 20


def build_customer_order_accepted_message(venta: Venta) -> str:
    subtotal = _as_money(venta.total)
    shipping = _as_money(venta.costo_envio)
    grand_total = subtotal + shipping

    lines = [
        'RAMON by Bosco',
        'COMPROBANTE DE VENTA',
        f'Pedido #{venta.id} | {_format_sale_datetime(venta)}',
        f'Metodo de pago: {venta.get_metodo_pago_display().upper()}',
        f'Estado: {venta.get_estado_display()}',
        '',
        'DATOS DEL CLIENTE',
        *_build_customer_lines(venta),
        f'Tipo de pedido: {venta.get_tipo_pedido_display()}',
        '',
        'DETALLE DEL PEDIDO',
        *_build_item_lines(venta),
        '',
        'RESUMEN',
        f'Subtotal productos: ${subtotal:.2f}',
    ]

    if venta.tipo_pedido == 'DOMICILIO':
        lines.append(f'Envio: ${shipping:.2f}')

    lines.append(f'Total pedido: ${grand_total:.2f}')

    if venta.metodo_pago == 'EFECTIVO':
        lines.extend(
            [
                '',
                'PAGO',
                f'Monto recibido: ${_as_money(venta.monto_recibido):.2f}',
                f'Cambio: ${_as_money(venta.cambio):.2f}',
            ]
        )

    lines.extend(
        [
            '',
            'Gracias por tu compra. Conserva este comprobante para tus registros.',
        ]
    )
    return '\n'.join(lines)


def send_customer_order_accepted_message(venta: Venta, *, raise_on_error: bool = False):
    customer_phone = venta.telefono_cliente_e164 or venta.telefono_cliente
    if not customer_phone:
        return None
    return send_whatsapp_text(
        customer_phone,
        build_customer_order_accepted_message(venta),
        raise_on_error=raise_on_error,
    )


def _build_item_lines(venta: Venta) -> list[str]:
    detail_lines = []
    detalles = list(venta.detalles.select_related('producto').all()[: MAX_RECEIPT_ITEMS + 1])
    visible_detalles = detalles[:MAX_RECEIPT_ITEMS]
    extra_count = max(len(detalles) - MAX_RECEIPT_ITEMS, 0)

    for detalle in visible_detalles:
        item_name = detalle.producto.nombre if detalle.producto_id else 'Item'
        unit_price = _as_money(detalle.precio_unitario)
        line_total = _as_money(detalle.subtotal)
        detail_lines.append(f'- {detalle.cantidad}x {item_name} | P/U: ${unit_price:.2f} | Subtotal: ${line_total:.2f}')
        if detalle.nota:
            detail_lines.append(f'  Nota: {detalle.nota}')

    if extra_count:
        detail_lines.append(f'- ... y {extra_count} item(s) mas')

    if not detail_lines:
        detail_lines.append('- Sin detalle')
    return detail_lines


def _build_customer_lines(venta: Venta) -> list[str]:
    cliente = venta.cliente
    lines = [f'Nombre: {_customer_name(venta)}']

    if cliente:
        if cliente.cedula_ruc:
            lines.append(f'CI/RUC: {cliente.cedula_ruc}')
        if cliente.telefono:
            lines.append(f'Telefono: {cliente.telefono}')
        if cliente.email:
            lines.append(f'Correo: {cliente.email}')
        if cliente.direccion:
            lines.append(f'Direccion: {cliente.direccion}')
    else:
        if venta.telefono_cliente:
            lines.append(f'Telefono: {venta.telefono_cliente}')
        if venta.email_cliente:
            lines.append(f'Correo: {venta.email_cliente}')
        if venta.direccion_envio:
            lines.append(f'Direccion: {venta.direccion_envio}')

    return lines


def _customer_name(venta: Venta) -> str:
    if venta.cliente and venta.cliente.nombre:
        return venta.cliente.nombre
    return venta.cliente_nombre or 'CONSUMIDOR FINAL'


def _format_sale_datetime(venta: Venta) -> str:
    sale_datetime = venta.fecha or timezone.now()
    return timezone.localtime(sale_datetime).strftime('%d/%m/%Y %H:%M')


def _as_money(value) -> Decimal:
    return Decimal(str(value or '0.00')).quantize(Decimal('0.01'))
