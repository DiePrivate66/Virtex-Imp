from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.shortcuts import redirect, render

from pos.application.cash_movements import (
    CashMovementError,
    delete_cash_movement,
    get_accounting_report_context,
    get_cash_movements_panel_context,
    register_cash_movement,
)

logger = logging.getLogger(__name__)


def panel_movimientos(request):
    if not request.user.is_authenticated:
        return redirect("pos_login")

    context = get_cash_movements_panel_context(request.user)
    if context is None:
        return redirect("pos_apertura")

    return render(request, "pos/movimientos_caja.html", context)


def api_registrar_movimiento(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "mensaje": "Metodo no permitido"}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({"status": "error", "mensaje": "No autorizado"}, status=401)

    try:
        data = json.loads(request.body)
        result = register_cash_movement(
            user=request.user,
            tipo=data.get("tipo", "EGRESO"),
            concepto=data.get("concepto", ""),
            descripcion=data.get("descripcion", ""),
            monto_raw=data.get("monto", 0),
        )
        label = "Ingreso" if result.tipo == "INGRESO" else "Egreso"
        return JsonResponse(
            {
                "status": "ok",
                "mensaje": f"{label} de ${result.monto} registrado",
                "id": result.id,
            }
        )
    except CashMovementError as exc:
        return JsonResponse({"status": "error", "mensaje": exc.message}, status=exc.status_code)
    except Exception:
        logger.exception("Error inesperado registrando movimiento de caja")
        return JsonResponse(
            {"status": "error", "mensaje": "No se pudo registrar el movimiento. Intenta nuevamente."},
            status=500,
        )


def api_eliminar_movimiento(request):
    if not request.user.is_authenticated:
        return JsonResponse({"status": "error", "mensaje": "No autorizado"}, status=401)

    try:
        data = json.loads(request.body)
        delete_cash_movement(movimiento_id=data.get("id"))
        return JsonResponse({"status": "ok", "mensaje": "Movimiento eliminado"})
    except CashMovementError as exc:
        return JsonResponse({"status": "error", "mensaje": exc.message}, status=exc.status_code)


def reporte_contadora(request):
    if not request.user.is_authenticated:
        return redirect("pos_login")

    if hasattr(request.user, "empleado") and request.user.empleado.rol != "ADMIN":
        return redirect("pos_index")

    context = get_accounting_report_context(
        desde=request.GET.get("desde"),
        hasta=request.GET.get("hasta"),
    )
    return render(request, "pos/reporte_contadora.html", context)
