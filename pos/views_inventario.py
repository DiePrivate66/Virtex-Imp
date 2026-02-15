"""
Vistas para control de inventario y reporte PDF.
"""
import json
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse

from django.db.models import Sum, F, Q
from django.utils import timezone
from .models import Producto, Inventario, MovimientoInventario


def panel_inventario(request):
    """Vista principal del inventario — lista todo el stock."""
    if not request.user.is_authenticated:
        return redirect('pos_login')
    
    # Auto-crear inventario para productos que no lo tienen
    productos_sin_inv = Producto.objects.filter(inventario__isnull=True)
    for prod in productos_sin_inv:
        Inventario.objects.create(producto=prod)
    
    inventarios = Inventario.objects.select_related('producto__categoria').all().order_by(
        'producto__categoria__nombre', 'producto__nombre'
    )
    
    # Estadísticas
    total_productos = inventarios.count()
    bajo_stock = inventarios.filter(stock_actual__lte=F('stock_minimo')).count()
    sin_stock = inventarios.filter(stock_actual__lte=0).count()
    
    context = {
        'inventarios': inventarios,
        'total_productos': total_productos,
        'bajo_stock': bajo_stock,
        'sin_stock': sin_stock,
    }
    return render(request, 'pos/inventario.html', context)


def api_movimiento_inventario(request):
    """API POST: registra un movimiento de stock (entrada, salida, ajuste, merma)."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Método no permitido'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=401)
    
    try:
        data = json.loads(request.body)
        producto = Producto.objects.get(id=data.get('producto_id'))
        inv, _ = Inventario.objects.get_or_create(producto=producto)
        
        tipo = data.get('tipo', 'ENTRADA')
        cantidad = int(data.get('cantidad', 0))
        concepto = data.get('concepto', '')
        
        if cantidad <= 0:
            return JsonResponse({'status': 'error', 'mensaje': 'La cantidad debe ser mayor a 0'}, status=400)
        
        stock_anterior = inv.stock_actual
        
        if tipo == 'ENTRADA':
            inv.stock_actual += cantidad
        elif tipo in ('SALIDA', 'MERMA'):
            inv.stock_actual -= cantidad
        elif tipo == 'AJUSTE':
            # El valor de cantidad es el NUEVO stock real
            inv.stock_actual = cantidad
            cantidad = cantidad - stock_anterior  # Diferencia para el log
        
        inv.save()
        
        MovimientoInventario.objects.create(
            producto=producto,
            tipo=tipo,
            cantidad=cantidad,
            stock_anterior=stock_anterior,
            stock_nuevo=inv.stock_actual,
            concepto=concepto,
            registrado_por=request.user,
        )
        
        return JsonResponse({
            'status': 'ok',
            'mensaje': f'{producto.nombre}: stock actualizado a {inv.stock_actual}',
            'stock_nuevo': inv.stock_actual,
        })
    except Producto.DoesNotExist:
        return JsonResponse({'status': 'error', 'mensaje': 'Producto no encontrado'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'mensaje': str(e)}, status=500)


def api_actualizar_minimo(request):
    """API POST: actualiza el stock mínimo y unidad de un producto."""
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=401)
    
    try:
        data = json.loads(request.body)
        inv = Inventario.objects.get(producto_id=data.get('producto_id'))
        
        if 'stock_minimo' in data:
            inv.stock_minimo = int(data['stock_minimo'])
        if 'unidad' in data:
            inv.unidad = data['unidad']
        
        inv.save()
        return JsonResponse({'status': 'ok', 'mensaje': 'Configuración guardada'})
    except Inventario.DoesNotExist:
        return JsonResponse({'status': 'error', 'mensaje': 'Inventario no encontrado'}, status=404)


def historial_inventario(request, producto_id):
    """Vista del historial de movimientos de un producto."""
    if not request.user.is_authenticated:
        return redirect('pos_login')
    
    producto = get_object_or_404(Producto, id=producto_id)
    movimientos = MovimientoInventario.objects.filter(producto=producto)[:100]
    inv, _ = Inventario.objects.get_or_create(producto=producto)
    
    return render(request, 'pos/historial_inventario.html', {
        'producto': producto,
        'inventario': inv,
        'movimientos': movimientos,
    })


def reporte_inventario_pdf(request):
    """Genera un reporte de inventario imprimible (HTML optimizado para print)."""
    if not request.user.is_authenticated:
        return redirect('pos_login')
    
    inventarios = Inventario.objects.select_related('producto__categoria').all().order_by(
        'producto__categoria__nombre', 'producto__nombre'
    )
    
    bajo_stock = inventarios.filter(stock_actual__lte=F('stock_minimo'))
    sin_stock = inventarios.filter(stock_actual__lte=0)
    
    context = {
        'inventarios': inventarios,
        'bajo_stock': bajo_stock,
        'sin_stock': sin_stock,
        'total_productos': inventarios.count(),
        'ahora': timezone.localtime(),
        'usuario': request.user.get_full_name() or request.user.username,
    }
    return render(request, 'pos/print/reporte_inventario.html', context)
