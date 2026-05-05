from __future__ import annotations

from decimal import Decimal

from pos.models import Venta

from .whatsapp import send_whatsapp_text


def build_customer_order_accepted_message(venta: Venta) -> str:
    subtotal = _as_money(venta.total)
    shipping = _as_money(venta.costo_envio)
    grand_total = subtotal + shipping
    item_lines = _build_item_lines(venta)

    lines = [
        f'Pedido #{venta.id} confirmado por el local.',
        f'Cliente: {venta.cliente_nombre or "CONSUMIDOR FINAL"}',
        f'Tipo: {venta.tipo_pedido}',
        'Items:',
        *item_lines,
        f'Subtotal productos: ${subtotal:.2f}',
    ]

    if venta.tipo_pedido == 'DOMICILIO':
        lines.append(f'Envio: ${shipping:.2f}')
        if venta.direccion_envio:
            lines.append(f'Direccion: {venta.direccion_envio}')

    lines.extend(
        [
            f'Total pedido: ${grand_total:.2f}',
            f'Estado: {venta.get_estado_display()}',
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
    detalles = venta.detalles.select_related('producto').all()[:4]
    for detalle in detalles:
        item_name = detalle.producto.nombre if detalle.producto_id else 'Item'
        detail_lines.append(f'- {detalle.cantidad}x {item_name}')
    if not detail_lines:
        detail_lines.append('- Sin detalle')
    return detail_lines


def _as_money(value) -> Decimal:
    return Decimal(str(value or '0.00')).quantize(Decimal('0.01'))
