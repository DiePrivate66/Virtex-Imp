import json
import logging

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .models import CajaTurno, Venta
from .services import (
    PosServiceError,
    build_cierre_context,
    build_ticket_context,
    build_venta_context,
    build_web_orders_payload,
    get_open_cash_register,
    get_pos_index_context,
    get_web_orders_panel_context,
    register_pos_sale,
    update_web_order,
    user_is_pos_operator,
)

logger = logging.getLogger(__name__)


def pos_index(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    caja_abierta = get_open_cash_register(request.user)
    if not caja_abierta:
        return redirect('pos_apertura')

    return render(request, 'pos/index.html', get_pos_index_context(request.user))


@login_required(login_url='pos_login')
def registrar_venta(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Metodo no permitido'}, status=405)
    if not user_is_pos_operator(request.user):
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=403)

    try:
        data = json.loads(request.body)
        venta = register_pos_sale(request.user, data)
        return JsonResponse({'status': 'ok', 'mensaje': f'Venta #{venta.id} registrada', 'ticket_id': venta.id})
    except PosServiceError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)
    except Exception:
        logger.exception('Error inesperado registrando venta POS')
        return JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo registrar la venta. Intenta nuevamente.'},
            status=500,
        )


def panel_pedidos_web(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')
    if not user_is_pos_operator(request.user):
        raise PermissionDenied('No autorizado para ver pedidos web')

    return render(request, 'pos/pedidos_web.html', get_web_orders_panel_context())


@login_required(login_url='pos_login')
def api_actualizar_pedido(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Metodo no permitido'}, status=405)
    if not user_is_pos_operator(request.user):
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=403)

    try:
        data = json.loads(request.body)
        venta = update_web_order(data)
        return JsonResponse(
            {
                'status': 'ok',
                'estado': venta.estado,
                'estado_display': venta.get_estado_display(),
            }
        )
    except Venta.DoesNotExist:
        return JsonResponse({'status': 'error', 'mensaje': 'Pedido no encontrado'}, status=404)
    except PosServiceError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)
    except Exception:
        logger.exception('Error inesperado actualizando pedido web')
        return JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo actualizar el pedido. Intenta nuevamente.'},
            status=500,
        )


@login_required(login_url='pos_login')
def api_pedidos_web_json(request):
    if not user_is_pos_operator(request.user):
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=403)
    return JsonResponse(build_web_orders_payload())


@login_required(login_url='pos_login')
def imprimir_ticket(request, venta_id):
    if not user_is_pos_operator(request.user):
        raise PermissionDenied('No autorizado para imprimir tickets')

    venta = get_object_or_404(Venta, id=venta_id)
    return render(request, 'pos/print/ticket_consumidor.html', build_ticket_context(venta))


@login_required(login_url='pos_login')
def imprimir_comanda(request, venta_id):
    if not user_is_pos_operator(request.user):
        raise PermissionDenied('No autorizado para imprimir comandas')

    venta = get_object_or_404(Venta, id=venta_id)
    return render(request, 'pos/print/comanda_cocina.html', build_venta_context(venta))


@login_required(login_url='pos_login')
def imprimir_venta_completa(request, venta_id):
    if not user_is_pos_operator(request.user):
        raise PermissionDenied('No autorizado para imprimir ventas')

    venta = get_object_or_404(Venta, id=venta_id)
    return render(request, 'pos/print/venta_completa.html', build_venta_context(venta))


@login_required(login_url='pos_login')
def imprimir_cierre(request, caja_id):
    if not user_is_pos_operator(request.user):
        raise PermissionDenied('No autorizado para imprimir cierres')

    caja = get_object_or_404(CajaTurno, id=caja_id)
    return render(request, 'pos/print/reporte_cierre.html', build_cierre_context(caja))


@login_required(login_url='pos_login')
def imprimir_etiqueta_delivery(request, venta_id):
    if not user_is_pos_operator(request.user):
        raise PermissionDenied('No autorizado para imprimir etiquetas')

    venta = get_object_or_404(Venta, id=venta_id)
    return render(request, 'pos/print/etiqueta_delivery.html', build_venta_context(venta))
