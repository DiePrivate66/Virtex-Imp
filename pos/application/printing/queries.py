from __future__ import annotations

from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from pos.models import MovimientoCaja, Venta


def build_ticket_context(venta: Venta) -> dict:
    subtotal_sin_iva = (venta.total / Decimal('1.15')).quantize(Decimal('0.01'))
    iva_valor = (venta.total - subtotal_sin_iva).quantize(Decimal('0.01'))
    return {
        'venta': venta,
        'subtotal_sin_iva': subtotal_sin_iva,
        'iva_valor': iva_valor,
    }


def build_sale_context(venta: Venta) -> dict:
    return {'venta': venta}


def build_cash_closing_context(caja) -> dict:
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
