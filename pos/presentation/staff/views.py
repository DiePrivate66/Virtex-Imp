from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.shortcuts import redirect, render

from pos.application.staff import StaffError, get_employee_list, register_attendance, save_employee

logger = logging.getLogger(__name__)


def lista_empleados(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    empleados = get_employee_list()
    return render(request, 'pos/empleados/lista.html', {'empleados': empleados})


def guardar_empleado(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'status': 'error'}, status=400)

    data = json.loads(request.body)
    try:
        save_employee(data)
        return JsonResponse({'status': 'ok'})
    except StaffError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)
    except Exception:
        logger.exception('Error inesperado guardando empleado')
        return JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo guardar el empleado. Intenta nuevamente.'},
            status=500,
        )


def registrar_asistencia(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error'}, status=400)

    data = json.loads(request.body)
    try:
        mensaje = register_attendance(data.get('pin'), data.get('accion'))
        return JsonResponse({'status': 'ok', 'mensaje': mensaje})
    except StaffError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)
