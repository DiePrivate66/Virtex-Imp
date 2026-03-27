from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, F, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.utils import timezone

from pos.models import Asistencia, DetalleVenta, MovimientoCaja, Venta


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

    ventas = Venta.objects.filter(fecha__date__gte=desde, fecha__date__lte=hasta).exclude(
        estado='CANCELADO'
    )
    total_ventas = ventas.aggregate(t=Sum('total'))['t'] or Decimal('0')
    num_ventas = ventas.count()
    ticket_promedio = ventas.aggregate(a=Avg('total'))['a'] or Decimal('0')

    ventas_hoy = Venta.objects.filter(fecha__date=hoy).exclude(estado='CANCELADO')
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

    movimientos = MovimientoCaja.objects.filter(fecha__date__gte=desde, fecha__date__lte=hasta)
    total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0')
    total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0')

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
    }
