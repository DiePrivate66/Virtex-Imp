from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render

from pos.application.printing import build_cash_closing_context, build_sale_context, build_ticket_context
from pos.application.staff import user_is_pos_operator
from pos.models import CajaTurno, Venta


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
    return render(request, 'pos/print/comanda_cocina.html', build_sale_context(venta))


@login_required(login_url='pos_login')
def imprimir_venta_completa(request, venta_id):
    if not user_is_pos_operator(request.user):
        raise PermissionDenied('No autorizado para imprimir ventas')

    venta = get_object_or_404(Venta, id=venta_id)
    return render(request, 'pos/print/venta_completa.html', build_sale_context(venta))


@login_required(login_url='pos_login')
def imprimir_cierre(request, caja_id):
    if not user_is_pos_operator(request.user):
        raise PermissionDenied('No autorizado para imprimir cierres')

    caja = get_object_or_404(CajaTurno, id=caja_id)
    return render(request, 'pos/print/reporte_cierre.html', build_cash_closing_context(caja))


@login_required(login_url='pos_login')
def imprimir_etiqueta_delivery(request, venta_id):
    if not user_is_pos_operator(request.user):
        raise PermissionDenied('No autorizado para imprimir etiquetas')

    venta = get_object_or_404(Venta, id=venta_id)
    return render(request, 'pos/print/etiqueta_delivery.html', build_sale_context(venta))
