from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render

from pos.application.analytics import build_analytics_dashboard_context
from pos.application.sales import (
    PosSaleError,
    resolve_accounting_adjustment,
    resolve_payment_exception,
    resolve_post_close_replay_alert,
)


def dashboard_analytics(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    if hasattr(request.user, 'empleado') and request.user.empleado.rol != 'ADMIN':
        return redirect('pos_index')

    return render(
        request,
        'pos/dashboard.html',
        build_analytics_dashboard_context(
            periodo=request.GET.get('periodo', 'semana'),
            desde_param=request.GET.get('desde'),
            hasta_param=request.GET.get('hasta'),
        ),
    )


def resolver_excepcion_pago(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    if request.method != 'POST':
        return redirect('dashboard_analytics')

    if hasattr(request.user, 'empleado') and request.user.empleado.rol != 'ADMIN' and not request.user.is_superuser:
        return redirect('pos_index')

    try:
        resolve_payment_exception(
            audit_log_id=int(request.POST.get('audit_log_id', '0') or 0),
            user=request.user,
            resolution_note=request.POST.get('resolution_note', ''),
            resolution_action=request.POST.get('resolution_action', ''),
            resolution_reference=request.POST.get('resolution_reference', ''),
        )
        messages.success(request, 'La excepcion de pago fue resuelta correctamente.')
    except (ValueError, PosSaleError):
        messages.error(request, 'No se pudo resolver la excepcion de pago con la informacion enviada.')
        return redirect('dashboard_analytics')

    return redirect('dashboard_analytics')


def resolver_ajuste_contable(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    if request.method != 'POST':
        return redirect('dashboard_analytics')

    if hasattr(request.user, 'empleado') and request.user.empleado.rol != 'ADMIN' and not request.user.is_superuser:
        return redirect('pos_index')

    try:
        resolve_accounting_adjustment(
            adjustment_id=int(request.POST.get('adjustment_id', '0') or 0),
            user=request.user,
            resolution_note=request.POST.get('resolution_note', ''),
            resolution_reference=request.POST.get('resolution_reference', ''),
            settlement_mode=request.POST.get('settlement_mode', ''),
        )
        messages.success(request, 'El ajuste contable fue marcado como resuelto.')
    except ValueError:
        messages.error(request, 'No se pudo resolver el ajuste contable con la informacion enviada.')
        return redirect('dashboard_analytics')
    except PosSaleError as exc:
        messages.error(request, exc.message)
        return redirect('dashboard_analytics')

    return redirect('dashboard_analytics')


def resolver_alerta_replay(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    if request.method != 'POST':
        return redirect('dashboard_analytics')

    if hasattr(request.user, 'empleado') and request.user.empleado.rol != 'ADMIN' and not request.user.is_superuser:
        return redirect('pos_index')

    try:
        resolve_post_close_replay_alert(
            audit_log_id=int(request.POST.get('audit_log_id', '0') or 0),
            user=request.user,
            resolution_note=request.POST.get('resolution_note', ''),
        )
        messages.success(request, 'La alerta temporal de replay fue marcada como revisada.')
    except (ValueError, PosSaleError):
        messages.error(request, 'No se pudo resolver la alerta temporal con la informacion enviada.')
        return redirect('dashboard_analytics')

    return redirect('dashboard_analytics')
