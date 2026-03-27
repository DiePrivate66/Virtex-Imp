from __future__ import annotations

from django.shortcuts import redirect, render

from pos.application.analytics import build_analytics_dashboard_context


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
