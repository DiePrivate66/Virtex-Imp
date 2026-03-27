from __future__ import annotations

import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render

from pos.application.sales import (
    PosSaleError,
    get_pos_home_context,
    get_user_open_cash_register,
    register_sale,
)
from pos.application.staff import user_is_pos_operator

logger = logging.getLogger(__name__)


def pos_index(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    caja_abierta = get_user_open_cash_register(request.user)
    if not caja_abierta:
        return redirect('pos_apertura')

    return render(request, 'pos/index.html', get_pos_home_context(request.user))


@login_required(login_url='pos_login')
def registrar_venta(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Metodo no permitido'}, status=405)
    if not user_is_pos_operator(request.user):
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=403)

    try:
        data = json.loads(request.body)
        venta = register_sale(request.user, data)
        return JsonResponse({'status': 'ok', 'mensaje': f'Venta #{venta.id} registrada', 'ticket_id': venta.id})
    except PosSaleError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)
    except Exception:
        logger.exception('Error inesperado registrando venta POS')
        return JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo registrar la venta. Intenta nuevamente.'},
            status=500,
        )
