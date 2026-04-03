from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from pos.application.inventory import (
    InventoryError,
    get_inventory_history_context,
    get_inventory_panel_context,
    get_inventory_report_context,
    register_inventory_movement,
    update_inventory_configuration,
)
from pos.models import Producto

logger = logging.getLogger(__name__)


def panel_inventario(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    return render(request, 'pos/inventario.html', get_inventory_panel_context(request.user))


def api_movimiento_inventario(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Metodo no permitido'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=401)

    try:
        data = json.loads(request.body)
        result = register_inventory_movement(
            producto_id=data.get('producto_id'),
            tipo=data.get('tipo', 'ENTRADA'),
            cantidad_raw=data.get('cantidad', 0),
            concepto=data.get('concepto', ''),
            registrado_por=request.user,
        )
        return JsonResponse({
            'status': 'ok',
            'mensaje': f'{result.producto_nombre}: stock actualizado a {result.stock_nuevo}',
            'stock_nuevo': result.stock_nuevo,
        })
    except InventoryError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)
    except Exception:
        logger.exception('Error inesperado registrando movimiento de inventario')
        return JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo registrar el movimiento. Intenta nuevamente.'},
            status=500,
        )


def api_actualizar_minimo(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=401)

    try:
        data = json.loads(request.body)
        update_inventory_configuration(
            producto_id=data.get('producto_id'),
            stock_minimo=data['stock_minimo'] if 'stock_minimo' in data else None,
            unidad=data['unidad'] if 'unidad' in data else None,
            user=request.user,
        )
        return JsonResponse({'status': 'ok', 'mensaje': 'Configuracion guardada'})
    except InventoryError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)


def historial_inventario(request, producto_id):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    get_object_or_404(Producto, id=producto_id)
    return render(request, 'pos/historial_inventario.html', get_inventory_history_context(producto_id, user=request.user))


def reporte_inventario_pdf(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    context = get_inventory_report_context(
        ahora=timezone.localtime(),
        usuario=request.user.get_full_name() or request.user.username,
        user=request.user,
    )
    return render(request, 'pos/print/reporte_inventario.html', context)
