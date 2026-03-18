"""
Vistas para movimientos de caja (ingresos/gastos) y reporte para contadora.
"""
import json
import logging
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse

from django.db.models import Sum, Q
from django.utils import timezone
from .models import CajaTurno, MovimientoCaja

logger = logging.getLogger(__name__)


def panel_movimientos(request):
    """Vista principal para registrar y ver ingresos/gastos del turno activo."""
    if not request.user.is_authenticated:
        return redirect('pos_login')
    
    turno = CajaTurno.objects.filter(usuario=request.user, fecha_cierre__isnull=True).first()
    if not turno:
        return redirect('pos_apertura')
    
    movimientos = MovimientoCaja.objects.filter(turno=turno)
    
    total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0.00')
    total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0.00')
    
    context = {
        'turno': turno,
        'movimientos': movimientos,
        'total_ingresos': total_ingresos,
        'total_egresos': total_egresos,
        'balance': total_ingresos - total_egresos,
        'conceptos_egreso': MovimientoCaja.CONCEPTOS_EGRESO,
        'conceptos_ingreso': MovimientoCaja.CONCEPTOS_INGRESO,
    }
    return render(request, 'pos/movimientos_caja.html', context)


def api_registrar_movimiento(request):
    """API POST: registra un ingreso o egreso de caja."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Método no permitido'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=401)
    
    try:
        data = json.loads(request.body)
        turno = CajaTurno.objects.filter(usuario=request.user, fecha_cierre__isnull=True).first()
        if not turno:
            return JsonResponse({'status': 'error', 'mensaje': 'No hay turno abierto'}, status=400)
        
        tipo = data.get('tipo', 'EGRESO')
        concepto = data.get('concepto', '')
        descripcion = data.get('descripcion', '')
        monto = Decimal(str(data.get('monto', 0)))
        
        if monto <= 0:
            return JsonResponse({'status': 'error', 'mensaje': 'El monto debe ser mayor a 0'}, status=400)
        if not concepto:
            return JsonResponse({'status': 'error', 'mensaje': 'Selecciona un concepto'}, status=400)
        
        mov = MovimientoCaja.objects.create(
            turno=turno,
            tipo=tipo,
            concepto=concepto,
            descripcion=descripcion,
            monto=monto,
            registrado_por=request.user,
        )
        
        return JsonResponse({
            'status': 'ok',
            'mensaje': f'{"Ingreso" if tipo == "INGRESO" else "Egreso"} de ${monto} registrado',
            'id': mov.id,
        })
    except Exception:
        logger.exception('Error inesperado registrando movimiento de caja')
        return JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo registrar el movimiento. Intenta nuevamente.'},
            status=500,
        )


def api_eliminar_movimiento(request):
    """API POST: elimina un movimiento (solo admin)."""
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=401)
    
    try:
        data = json.loads(request.body)
        mov = MovimientoCaja.objects.get(id=data.get('id'))
        mov.delete()
        return JsonResponse({'status': 'ok', 'mensaje': 'Movimiento eliminado'})
    except MovimientoCaja.DoesNotExist:
        return JsonResponse({'status': 'error', 'mensaje': 'Movimiento no encontrado'}, status=404)


def reporte_contadora(request):
    """Reporte semanal para la contadora — incluye ventas + movimientos + cierre. (SOLO ADMIN)"""
    if not request.user.is_authenticated:
        return redirect('pos_login')
    
    # Solo ADMIN puede ver reportes financieros
    if hasattr(request.user, 'empleado') and request.user.empleado.rol != 'ADMIN':
        return redirect('pos_index')
    
    # Obtener rango de fechas (por defecto: última semana)
    from datetime import timedelta
    hoy = timezone.localdate()
    desde = request.GET.get('desde', (hoy - timedelta(days=7)).isoformat())
    hasta = request.GET.get('hasta', hoy.isoformat())
    
    from .models import Venta
    
    ventas = Venta.objects.filter(fecha__date__gte=desde, fecha__date__lte=hasta).exclude(estado='CANCELADO')
    movimientos = MovimientoCaja.objects.filter(fecha__date__gte=desde, fecha__date__lte=hasta)
    turnos = CajaTurno.objects.filter(fecha_apertura__date__gte=desde, fecha_apertura__date__lte=hasta, fecha_cierre__isnull=False)
    
    # Totales de ventas por método
    total_efectivo = ventas.filter(metodo_pago='EFECTIVO').aggregate(t=Sum('total'))['t'] or Decimal('0')
    total_transferencia = ventas.filter(metodo_pago='TRANSFERENCIA').aggregate(t=Sum('total'))['t'] or Decimal('0')
    total_tarjeta = ventas.filter(metodo_pago='TARJETA').aggregate(t=Sum('total'))['t'] or Decimal('0')
    total_ventas = total_efectivo + total_transferencia + total_tarjeta
    
    # Totales de movimientos
    total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0')
    total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0')
    
    # Desglose de egresos por concepto
    egresos_por_concepto = movimientos.filter(tipo='EGRESO').values('concepto').annotate(
        total=Sum('monto')
    ).order_by('-total')
    
    # Sumar diferencias de cierres (sobrantes/faltantes)
    total_diferencia = turnos.aggregate(t=Sum('diferencia'))['t'] or Decimal('0')
    
    context = {
        'desde': desde,
        'hasta': hasta,
        'ventas': ventas,
        'movimientos': movimientos,
        'turnos': turnos,
        'total_efectivo': total_efectivo,
        'total_transferencia': total_transferencia,
        'total_tarjeta': total_tarjeta,
        'total_ventas': total_ventas,
        'total_ingresos': total_ingresos,
        'total_egresos': total_egresos,
        'egresos_por_concepto': egresos_por_concepto,
        'total_diferencia': total_diferencia,
        'num_ventas': ventas.count(),
        'num_turnos': turnos.count(),
    }
    return render(request, 'pos/reporte_contadora.html', context)
