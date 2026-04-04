from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, F, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.utils import timezone

from pos.models import AccountingAdjustment, Asistencia, AuditLog, DetalleVenta, MovimientoCaja, Venta


def _resolve_period(periodo: str, hoy, desde_param, hasta_param):
    if periodo == 'hoy':
        desde = hoy
    elif periodo == 'semana':
        desde = hoy - timedelta(days=7)
    elif periodo == 'mes':
        desde = hoy - timedelta(days=30)
    elif periodo == 'custom':
        desde = desde_param or (hoy - timedelta(days=7)).isoformat()
    else:
        desde = hoy - timedelta(days=7)

    return desde, hasta_param or hoy


def _build_previous_period_totals(hoy, desde):
    dias_periodo = (hoy - desde).days if isinstance(desde, type(hoy)) else 7
    periodo_anterior_desde = (
        (desde - timedelta(days=dias_periodo))
        if isinstance(desde, type(hoy))
        else hoy - timedelta(days=14)
    )
    periodo_anterior_hasta = desde if isinstance(desde, type(hoy)) else hoy - timedelta(days=7)
    return (
        Venta.objects.filter(
            fecha__date__gte=periodo_anterior_desde,
            fecha__date__lte=periodo_anterior_hasta,
            payment_status=Venta.PaymentStatus.PAID,
        )
        .exclude(estado='CANCELADO')
        .aggregate(t=Sum('total'))['t']
        or Decimal('0')
    )


def _build_top_products(ventas):
    return (
        DetalleVenta.objects.filter(venta__in=ventas)
        .values(nombre=F('producto__nombre'))
        .annotate(
            total_vendido=Sum('cantidad'),
            total_ingresos=Sum(F('cantidad') * F('precio_unitario')),
        )
        .order_by('-total_vendido')[:10]
    )


def _build_sales_by_hour(ventas):
    ventas_por_hora = (
        ventas.annotate(hora=ExtractHour('fecha'))
        .values('hora')
        .annotate(total=Sum('total'), cantidad=Count('id'))
        .order_by('hora')
    )
    hora_pico = None
    max_ventas_hora = 1
    for venta_hora in ventas_por_hora:
        if venta_hora['cantidad'] > max_ventas_hora:
            max_ventas_hora = venta_hora['cantidad']
            hora_pico = venta_hora['hora']
    return ventas_por_hora, hora_pico, max_ventas_hora


def _build_sales_by_day(ventas):
    ventas_por_dia = (
        ventas.annotate(dia=TruncDate('fecha'))
        .values('dia')
        .annotate(total=Sum('total'), cantidad=Count('id'))
        .order_by('dia')
    )
    mejor_dia = None
    max_venta_dia = Decimal('0')
    for venta_dia in ventas_por_dia:
        if venta_dia['total'] > max_venta_dia:
            max_venta_dia = venta_dia['total']
            mejor_dia = venta_dia['dia']
    return ventas_por_dia, mejor_dia, max_venta_dia


def _build_attendance_data(desde, hasta):
    asistencias = (
        Asistencia.objects.filter(fecha__gte=desde, fecha__lte=hasta)
        .select_related('empleado')
        .order_by('-fecha', '-hora_entrada')
    )
    data = []
    for asistencia in asistencias:
        horas = 0
        estado = 'Abierto'
        if asistencia.hora_salida:
            entrada = datetime.combine(datetime.today(), asistencia.hora_entrada)
            salida = datetime.combine(datetime.today(), asistencia.hora_salida)
            diff = salida - entrada
            horas = round(diff.total_seconds() / 3600, 2)
            estado = 'Cerrado'
        data.append(
            {
                'empleado': asistencia.empleado.nombre,
                'rol': asistencia.empleado.get_rol_display(),
                'fecha': asistencia.fecha,
                'entrada': asistencia.hora_entrada,
                'salida': asistencia.hora_salida,
                'horas': horas,
                'estado': estado,
            }
        )
    return data


def build_analytics_dashboard_context(periodo: str = 'semana', desde_param=None, hasta_param=None):
    hoy = timezone.localdate()
    desde, hasta = _resolve_period(periodo, hoy, desde_param, hasta_param)

    ventas = Venta.objects.filter(
        fecha__date__gte=desde,
        fecha__date__lte=hasta,
        payment_status=Venta.PaymentStatus.PAID,
    ).exclude(
        estado='CANCELADO'
    )
    total_ventas = ventas.aggregate(t=Sum('total'))['t'] or Decimal('0')
    num_ventas = ventas.count()
    ticket_promedio = ventas.aggregate(a=Avg('total'))['a'] or Decimal('0')

    ventas_hoy = Venta.objects.filter(fecha__date=hoy, payment_status=Venta.PaymentStatus.PAID).exclude(estado='CANCELADO')
    total_hoy = ventas_hoy.aggregate(t=Sum('total'))['t'] or Decimal('0')
    num_hoy = ventas_hoy.count()

    total_anterior = _build_previous_period_totals(hoy, desde)
    crecimiento = 0
    if total_anterior > 0:
        crecimiento = round(((total_ventas - total_anterior) / total_anterior) * 100, 1)

    top_productos = _build_top_products(ventas)
    ventas_por_hora, hora_pico, max_ventas_hora = _build_sales_by_hour(ventas)
    ventas_por_dia, mejor_dia, max_venta_dia = _build_sales_by_day(ventas)

    por_metodo = (
        ventas.values('metodo_pago')
        .annotate(total=Sum('total'), cantidad=Count('id'))
        .order_by('-total')
    )
    ventas_pos = ventas.filter(origen='POS').aggregate(t=Sum('total'), c=Count('id'))
    ventas_web = ventas.filter(origen='WEB').aggregate(t=Sum('total'), c=Count('id'))

    movimientos = MovimientoCaja.objects.filter(fecha__date__gte=desde, fecha__date__lte=hasta).exclude(concepto='VENTA')
    total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0')
    total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0')
    payment_exceptions_queryset = AuditLog.objects.filter(
        event_type='sale.orphan_payment_detected',
        requires_attention=True,
        resolved_at__isnull=True,
    ).select_related('location', 'actor_user')
    payment_exceptions_open = list(payment_exceptions_queryset.order_by('-created_at')[:10])

    replay_timeline_alerts_queryset = AuditLog.objects.filter(
        event_type='sale.post_close_replay_alert',
        requires_attention=True,
        resolved_at__isnull=True,
    ).select_related('location', 'actor_user')
    replay_timeline_alerts_open = list(replay_timeline_alerts_queryset.order_by('-created_at')[:10])

    chronology_estimated_sales_count = ventas.filter(chronology_estimated=True).count()

    refund_adjustments_queryset = AccountingAdjustment.objects.filter(
        account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY,
        status=AccountingAdjustment.Status.OPEN,
    ).select_related('location', 'sale', 'created_by')
    refund_adjustments_open = list(refund_adjustments_queryset.order_by('-effective_at', '-created_at')[:10])
    refund_adjustments_open_total = (
        refund_adjustments_queryset.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    )

    return {
        'periodo': periodo,
        'desde': desde,
        'hasta': hasta,
        'periods': [('hoy', 'Hoy'), ('semana', '7 dias'), ('mes', '30 dias')],
        'total_ventas': total_ventas,
        'num_ventas': num_ventas,
        'ticket_promedio': ticket_promedio,
        'total_hoy': total_hoy,
        'num_hoy': num_hoy,
        'crecimiento': crecimiento,
        'total_anterior': total_anterior,
        'top_productos': top_productos,
        'ventas_por_hora': ventas_por_hora,
        'ventas_por_dia': ventas_por_dia,
        'hora_pico': hora_pico,
        'max_ventas_hora': max_ventas_hora,
        'mejor_dia': mejor_dia,
        'max_venta_dia': max_venta_dia,
        'por_metodo': por_metodo,
        'ventas_pos': ventas_pos,
        'ventas_web': ventas_web,
        'total_egresos': total_egresos,
        'total_ingresos_extra': total_ingresos,
        'ganancia_estimada': total_ventas + total_ingresos - total_egresos,
        'asistencias': _build_attendance_data(desde, hasta),
        'payment_exceptions_open': payment_exceptions_open,
        'payment_exceptions_open_count': payment_exceptions_queryset.count(),
        'replay_timeline_alerts_open': replay_timeline_alerts_open,
        'replay_timeline_alerts_open_count': replay_timeline_alerts_queryset.count(),
        'chronology_estimated_sales_count': chronology_estimated_sales_count,
        'refund_adjustments_open': refund_adjustments_open,
        'refund_adjustments_open_count': refund_adjustments_queryset.count(),
        'refund_adjustments_open_total': refund_adjustments_open_total,
    }
