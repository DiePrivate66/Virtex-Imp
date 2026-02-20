"""
Dashboard de Analíticas para el dueño del negocio.
Top productos, horas pico, ticket promedio, comparativas.
"""
from decimal import Decimal
from datetime import timedelta, datetime
from django.shortcuts import render, redirect
from django.db.models import Sum, Count, Avg, F, Q
from django.db.models.functions import ExtractHour, ExtractWeekDay, TruncDate
from django.utils import timezone
from .models import Venta, DetalleVenta, Producto, CajaTurno, MovimientoCaja


def dashboard_analytics(request):
    """Dashboard principal — analíticas del negocio (SOLO ADMIN)."""
    if not request.user.is_authenticated:
        return redirect('pos_login')
    
    # Solo ADMIN puede ver analytics
    if hasattr(request.user, 'empleado') and request.user.empleado.rol != 'ADMIN':
        return redirect('pos_index')
    
    hoy = timezone.localdate()
    
    # Períodos
    periodo = request.GET.get('periodo', 'semana')
    if periodo == 'hoy':
        desde = hoy
    elif periodo == 'semana':
        desde = hoy - timedelta(days=7)
    elif periodo == 'mes':
        desde = hoy - timedelta(days=30)
    elif periodo == 'custom':
        desde = request.GET.get('desde', (hoy - timedelta(days=7)).isoformat())
    else:
        desde = hoy - timedelta(days=7)
    
    hasta = request.GET.get('hasta', hoy)
    
    ventas = Venta.objects.filter(
        fecha__date__gte=desde, 
        fecha__date__lte=hasta
    ).exclude(estado='CANCELADO')
    
    # ═══════ MÉTRICAS GENERALES ═══════
    total_ventas = ventas.aggregate(t=Sum('total'))['t'] or Decimal('0')
    num_ventas = ventas.count()
    ticket_promedio = ventas.aggregate(a=Avg('total'))['a'] or Decimal('0')
    
    # Ventas de hoy
    ventas_hoy = Venta.objects.filter(fecha__date=hoy).exclude(estado='CANCELADO')
    total_hoy = ventas_hoy.aggregate(t=Sum('total'))['t'] or Decimal('0')
    num_hoy = ventas_hoy.count()
    
    # Comparativa con período anterior
    dias_periodo = (hoy - desde).days if isinstance(desde, type(hoy)) else 7
    periodo_anterior_desde = (desde - timedelta(days=dias_periodo)) if isinstance(desde, type(hoy)) else hoy - timedelta(days=14)
    periodo_anterior_hasta = desde if isinstance(desde, type(hoy)) else hoy - timedelta(days=7)
    
    total_anterior = Venta.objects.filter(
        fecha__date__gte=periodo_anterior_desde,
        fecha__date__lte=periodo_anterior_hasta
    ).exclude(estado='CANCELADO').aggregate(t=Sum('total'))['t'] or Decimal('0')
    
    crecimiento = 0
    if total_anterior > 0:
        crecimiento = round(((total_ventas - total_anterior) / total_anterior) * 100, 1)
    
    # ═══════ TOP PRODUCTOS ═══════
    top_productos = DetalleVenta.objects.filter(
        venta__in=ventas
    ).values(
        nombre=F('producto__nombre')
    ).annotate(
        total_vendido=Sum('cantidad'),
        total_ingresos=Sum(F('cantidad') * F('precio_unitario'))
    ).order_by('-total_vendido')[:10]
    
    # ═══════ VENTAS POR HORA ═══════
    ventas_por_hora = ventas.annotate(
        hora=ExtractHour('fecha')
    ).values('hora').annotate(
        total=Sum('total'),
        cantidad=Count('id')
    ).order_by('hora')
    
    # Encontrar hora pico
    hora_pico = None
    max_ventas_hora = 1
    for vh in ventas_por_hora:
        if vh['cantidad'] > max_ventas_hora:
            max_ventas_hora = vh['cantidad']
            hora_pico = vh['hora']
    
    # ═══════ VENTAS POR DÍA ═══════
    ventas_por_dia = ventas.annotate(
        dia=TruncDate('fecha')
    ).values('dia').annotate(
        total=Sum('total'),
        cantidad=Count('id')
    ).order_by('dia')
    
    # Mejor día
    mejor_dia = None
    max_venta_dia = Decimal('0')
    for vd in ventas_por_dia:
        if vd['total'] > max_venta_dia:
            max_venta_dia = vd['total']
            mejor_dia = vd['dia']
    
    # ═══════ DESGLOSE POR MÉTODO DE PAGO ═══════
    por_metodo = ventas.values('metodo_pago').annotate(
        total=Sum('total'),
        cantidad=Count('id')
    ).order_by('-total')
    
    # ═══════ POS vs WEB ═══════
    ventas_pos = ventas.filter(origen='POS').aggregate(
        t=Sum('total'), c=Count('id')
    )
    ventas_web = ventas.filter(origen='WEB').aggregate(
        t=Sum('total'), c=Count('id')
    )
    
    # ═══════ EGRESOS DEL PERÍODO ═══════
    movimientos = MovimientoCaja.objects.filter(
        fecha__date__gte=desde, fecha__date__lte=hasta
    )
    total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0')
    total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0')
    
    # Ganancia estimada (ventas + ingresos extra - egresos)
    ganancia_estimada = total_ventas + total_ingresos - total_egresos
    
    # Períodos para el template
    periods = [
        ('hoy', 'Hoy'),
        ('semana', '7 días'),
        ('mes', '30 días'),
    ]
    
    # ═══════ ASISTENCIA DE EMPLEADOS ═══════
    from .models import Asistencia
    
    asistencias = Asistencia.objects.filter(
        fecha__gte=desde, 
        fecha__lte=hasta
    ).select_related('empleado').order_by('-fecha', '-hora_entrada')

    # Calcular horas trabajadas
    asistencia_data = []
    for a in asistencias:
        horas = 0
        estado = 'Abierto'
        if a.hora_salida:
            # Calcular diferencia
            entrada = datetime.combine(datetime.today(), a.hora_entrada)
            salida = datetime.combine(datetime.today(), a.hora_salida)
            diff = salida - entrada
            horas = round(diff.total_seconds() / 3600, 2)
            estado = 'Cerrado'
        
        asistencia_data.append({
            'empleado': a.empleado.nombre,
            'rol': a.empleado.get_rol_display(),
            'fecha': a.fecha,
            'entrada': a.hora_entrada,
            'salida': a.hora_salida,
            'horas': horas,
            'estado': estado
        })

    context = {
        # Período
        'periodo': periodo,
        'desde': desde,
        'hasta': hasta,
        'periods': periods,
        
        # Métricas generales
        'total_ventas': total_ventas,
        'num_ventas': num_ventas,
        'ticket_promedio': ticket_promedio,
        'total_hoy': total_hoy,
        'num_hoy': num_hoy,
        'crecimiento': crecimiento,
        'total_anterior': total_anterior,
        
        # Rankings
        'top_productos': top_productos,
        'ventas_por_hora': ventas_por_hora,
        'ventas_por_dia': ventas_por_dia,
        'hora_pico': hora_pico,
        'max_ventas_hora': max_ventas_hora,
        'mejor_dia': mejor_dia,
        'max_venta_dia': max_venta_dia,
        
        # Desglose
        'por_metodo': por_metodo,
        'ventas_pos': ventas_pos,
        'ventas_web': ventas_web,
        
        # Flujo de caja
        'total_egresos': total_egresos,
        'total_ingresos_extra': total_ingresos,
        'ganancia_estimada': ganancia_estimada,
        
        # Personal
        'asistencias': asistencia_data,
    }
    return render(request, 'pos/dashboard.html', context)
