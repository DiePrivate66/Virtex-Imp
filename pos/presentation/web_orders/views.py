from __future__ import annotations

import json
import logging

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect, render

from pos.application.web_orders import (
    WebOrderTransitionError,
    apply_web_order_update,
    build_web_orders_payload,
    get_web_orders_panel_context,
)
from pos.application.staff import user_is_pos_operator

logger = logging.getLogger(__name__)


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
        venta = apply_web_order_update(data)
        return JsonResponse(
            {
                'status': 'ok',
                'estado': venta.estado,
                'estado_display': venta.get_estado_display(),
            }
        )
    except WebOrderTransitionError as exc:
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
