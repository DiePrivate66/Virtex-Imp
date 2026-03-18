from __future__ import annotations

import re
import threading
from decimal import Decimal

from django.contrib.auth.models import Group
from django.core.mail import send_mail
from django.db.models import Count, Sum
from django.template.loader import render_to_string
from django.utils import timezone

from .models import CajaTurno, Categoria, Cliente, DetalleVenta, MovimientoCaja, Producto, Venta

ALLOWED_POS_GROUPS = {'Cajero', 'Admin'}


class PosServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def user_is_pos_operator(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    _sync_user_groups_from_employee(user)
    return user.groups.filter(name__in=ALLOWED_POS_GROUPS).exists()


def get_open_cash_register(user):
    return CajaTurno.objects.filter(usuario=user, fecha_cierre__isnull=True).first()


def get_pos_index_context(user):
    return {
        'categorias': Categoria.objects.all(),
        'productos': Producto.objects.filter(activo=True),
        'caja': get_open_cash_register(user),
        'rol': getattr(getattr(user, 'empleado', None), 'rol', 'OTRO'),
    }


def register_pos_sale(user, data: dict) -> Venta:
    cedula_input = (data.get('cliente_cedula') or '').strip()
    consumidor_final = bool(data.get('consumidor_final'))
    metodo_pago = (data.get('metodo_pago') or '').upper().strip()
    total_venta = Decimal(str(data.get('total') or 0)).quantize(Decimal('0.01'))
    referencia_pago = _normalize_reference(data.get('referencia_pago'))
    tarjeta_tipo = _normalize_simple_text(data.get('tarjeta_tipo'), 12)
    tarjeta_marca = _normalize_simple_text(data.get('tarjeta_marca'), 20)

    turno_activo = get_open_cash_register(user)
    if not turno_activo:
        raise PosServiceError('No hay caja activa para registrar ventas', status_code=400)

    if metodo_pago not in {'EFECTIVO', 'TRANSFERENCIA', 'TARJETA'}:
        raise PosServiceError('Metodo de pago invalido', status_code=400)

    if total_venta <= 0:
        raise PosServiceError('El total de la venta debe ser mayor a 0', status_code=400)

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
        _send_sale_receipt_email_async(venta, cliente.email)

    return venta


def timed_out_quote_count() -> int:
    return Venta.objects.filter(
        origen='WEB',
        estado='PENDIENTE_COTIZACION',
        delivery_quote_deadline_at__isnull=False,
        delivery_quote_deadline_at__lt=timezone.now(),
    ).count()


def get_web_orders_panel_context(limit: int = 50):
    return {
        'pedidos': Venta.objects.filter(origen='WEB').exclude(estado='CANCELADO').order_by('-fecha')[:limit],
        'timed_out_quote_count': timed_out_quote_count(),
    }


def update_web_order(data: dict) -> Venta:
    pedido_id = data.get('pedido_id')
    if not pedido_id:
        raise PosServiceError('Pedido no encontrado', status_code=404)

    venta = Venta.objects.get(id=pedido_id, origen='WEB')
    update_fields: list[str] = []

    nuevo_estado = data.get('estado')
    if nuevo_estado:
        valid_states = {choice for choice, _ in Venta.ESTADO}
        if nuevo_estado not in valid_states:
            raise PosServiceError('Estado invalido', status_code=400)
        venta.estado = nuevo_estado
        update_fields.append('estado')

    costo = data.get('costo_envio')
    if costo is not None:
        costo_decimal = Decimal(str(costo))
        if costo_decimal < 0:
            raise PosServiceError('Costo de envio invalido', status_code=400)
        venta.costo_envio = costo_decimal
        update_fields.append('costo_envio')

    if update_fields:
        venta.save(update_fields=update_fields)

    return venta


def build_web_orders_payload(limit: int = 50):
    pedidos = (
        Venta.objects.filter(origen='WEB')
        .exclude(estado__in=['CANCELADO', 'LISTO'])
        .prefetch_related('detalles__producto')
        .order_by('-fecha')[:limit]
    )
    data = []
    for pedido in pedidos:
        items = [
            {
                'nombre': detalle.producto.nombre,
                'cantidad': detalle.cantidad,
                'nota': detalle.nota,
                'subtotal': str(detalle.subtotal),
            }
            for detalle in pedido.detalles.all()
        ]
        data.append(
            {
                'id': pedido.id,
                'estado': pedido.estado,
                'estado_display': pedido.get_estado_display(),
                'estado_pago': pedido.estado_pago,
                'cliente_nombre': pedido.cliente_nombre,
                'telefono': pedido.telefono_cliente,
                'direccion': pedido.direccion_envio,
                'tipo_pedido': pedido.tipo_pedido,
                'tipo_pedido_display': pedido.get_tipo_pedido_display(),
                'metodo_pago': pedido.metodo_pago,
                'metodo_pago_display': pedido.get_metodo_pago_display(),
                'referencia_pago': pedido.referencia_pago,
                'tarjeta_tipo': pedido.tarjeta_tipo,
                'tarjeta_marca': pedido.tarjeta_marca,
                'total': str(pedido.total),
                'costo_envio': str(pedido.costo_envio),
                'comprobante': pedido.comprobante_foto.url if pedido.comprobante_foto else None,
                'fecha': pedido.fecha.strftime('%H:%M'),
                'items': items,
            }
        )
    return {
        'pedidos': data,
        'count': len(data),
        'timed_out_quote_count': timed_out_quote_count(),
    }


def build_ticket_context(venta: Venta):
    subtotal_sin_iva = (venta.total / Decimal('1.15')).quantize(Decimal('0.01'))
    iva_valor = (venta.total - subtotal_sin_iva).quantize(Decimal('0.01'))
    return {
        'venta': venta,
        'subtotal_sin_iva': subtotal_sin_iva,
        'iva_valor': iva_valor,
    }


def build_venta_context(venta: Venta):
    return {'venta': venta}


def build_cierre_context(caja: CajaTurno):
    ventas = Venta.objects.filter(turno=caja)

    total_efectivo = ventas.filter(metodo_pago='EFECTIVO').aggregate(t=Sum('total'))['t'] or 0
    total_transferencia = ventas.filter(metodo_pago='TRANSFERENCIA').aggregate(t=Sum('total'))['t'] or 0
    total_tarjeta = ventas.filter(metodo_pago='TARJETA').aggregate(t=Sum('total'))['t'] or 0

    total_ventas = total_efectivo + total_transferencia + total_tarjeta
    total_ingresos_caja = (
        MovimientoCaja.objects.filter(turno=caja, tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or 0
    )
    total_egresos_caja = (
        MovimientoCaja.objects.filter(turno=caja, tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or 0
    )
    esperado = caja.base_inicial + total_efectivo + total_ingresos_caja - total_egresos_caja

    conteo_detalle = []
    if caja.conteo_billetes:
        for denom, cantidad in sorted(caja.conteo_billetes.items(), key=lambda item: float(item[0]), reverse=True):
            subtotal = float(denom) * int(cantidad)
            conteo_detalle.append((denom, cantidad, subtotal))

    tarjetas_por_referencia = list(
        ventas.filter(metodo_pago='TARJETA')
        .exclude(referencia_pago='')
        .values('referencia_pago', 'tarjeta_tipo', 'tarjeta_marca')
        .annotate(cantidad=Count('id'), total=Sum('total'))
        .order_by('-cantidad', 'referencia_pago')
    )

    return {
        'caja': caja,
        'cajero_nombre': caja.usuario.get_full_name() or caja.usuario.username,
        'total_efectivo': total_efectivo,
        'total_transferencia': total_transferencia,
        'total_tarjeta': total_tarjeta,
        'num_efectivo': ventas.filter(metodo_pago='EFECTIVO').count(),
        'num_transferencia': ventas.filter(metodo_pago='TRANSFERENCIA').count(),
        'num_tarjeta': ventas.filter(metodo_pago='TARJETA').count(),
        'num_ventas': ventas.count(),
        'total_ventas': total_ventas,
        'esperado': esperado,
        'total_ingresos_caja': total_ingresos_caja,
        'total_egresos_caja': total_egresos_caja,
        'conteo_detalle': conteo_detalle,
        'tarjetas_por_referencia': tarjetas_por_referencia,
        'ahora': timezone.now(),
    }


def _resolve_customer(data: dict, consumidor_final: bool, cedula_input: str):
    if consumidor_final:
        return None

    if data.get('cliente_id'):
        cliente = Cliente.objects.filter(id=data.get('cliente_id')).first()
        if not cliente:
            raise PosServiceError('Cliente no encontrado', status_code=400)
        if not _is_valid_identity(cliente.cedula_ruc):
            raise PosServiceError('C.I/RUC invalido (10 o 13 digitos)', status_code=400)
        return cliente

    if not _is_valid_identity(cedula_input):
        raise PosServiceError('C.I/RUC invalido (10 o 13 digitos)', status_code=400)

    return None


def _validate_card_payment(total_venta: Decimal, referencia_pago: str, tarjeta_tipo: str):
    if len(referencia_pago) < 6:
        raise PosServiceError('Referencia de tarjeta obligatoria (minimo 6 caracteres)', status_code=400)
    if not tarjeta_tipo:
        raise PosServiceError('Tipo de tarjeta obligatorio (credito o debito)', status_code=400)
    if tarjeta_tipo not in {'CREDITO', 'DEBITO'}:
        raise PosServiceError('Tipo de tarjeta invalido', status_code=400)

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
        raise PosServiceError(
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


def _send_sale_receipt_email_async(venta: Venta, recipient_email: str):
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


def _sync_user_groups_from_employee(user):
    empleado = getattr(user, 'empleado', None)
    if not empleado:
        return

    admin_group, _ = Group.objects.get_or_create(name='Admin')
    cajero_group, _ = Group.objects.get_or_create(name='Cajero')
    current_names = set(user.groups.values_list('name', flat=True))

    if empleado.rol == 'ADMIN':
        if current_names != {'Admin'}:
            user.groups.remove(admin_group, cajero_group)
            user.groups.add(admin_group)
        return

    if empleado.rol == 'CAJERO':
        if current_names != {'Cajero'}:
            user.groups.remove(admin_group, cajero_group)
            user.groups.add(cajero_group)
        return

    if current_names.intersection(ALLOWED_POS_GROUPS):
        user.groups.remove(admin_group, cajero_group)


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
