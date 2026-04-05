from __future__ import annotations

import json

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render

from pos.application.analytics import (
    OfflineLimboActionError,
    build_analytics_dashboard_context,
    build_offline_limbo_context,
    build_offline_limbo_payload,
    build_offline_segment_detail_payload,
    execute_offline_limbo_action,
    execute_offline_segment_action,
)
from pos.application.sales import (
    PosSaleError,
    resolve_accounting_adjustment,
    resolve_payment_exception,
    resolve_post_close_replay_alert,
)


def dashboard_analytics(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    return render(
        request,
        'pos/dashboard.html',
        build_analytics_dashboard_context(
            periodo=request.GET.get('periodo', 'semana'),
            desde_param=request.GET.get('desde'),
            hasta_param=request.GET.get('hasta'),
            offline_action_segment_id=request.GET.get('offline_action_segment_id', ''),
            offline_action_type=request.GET.get('offline_action_type', ''),
            offline_action_organization=request.GET.get('offline_action_organization', ''),
            offline_action_location=request.GET.get('offline_action_location', ''),
            offline_action_actor=request.GET.get('offline_action_actor', ''),
            offline_action_segment_status=request.GET.get('offline_action_segment_status', ''),
            offline_action_result=request.GET.get('offline_action_result', ''),
        ),
    )


def dashboard_offline_limbo(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    context = build_offline_limbo_context()
    context['initial_segment_id'] = str(request.GET.get('segment_id', '') or '').strip()
    return render(
        request,
        'pos/offline_limbo.html',
        context,
    )


def dashboard_offline_limbo_json(request):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    return JsonResponse(build_offline_limbo_payload(request.GET.get('segment_id', '')))


def dashboard_offline_limbo_segment_json(request):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    segment_id = request.GET.get('segment_id', '')
    try:
        return JsonResponse(build_offline_segment_detail_payload(segment_id))
    except ValueError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)


def dashboard_offline_limbo_segment_revalidate_json(request):
    return _execute_offline_segment_action_json(request, action='revalidate_footer')


def dashboard_offline_limbo_segment_review_json(request):
    return _execute_offline_segment_action_json(request, action='mark_operational_review')


def dashboard_offline_limbo_reconcile_json(request):
    return _execute_offline_limbo_action_json(request, action='reconcile_sidecar')


def dashboard_offline_limbo_reseal_json(request):
    return _execute_offline_limbo_action_json(request, action='reseal_segment')


def dashboard_offline_limbo_seal_json(request):
    return _execute_offline_limbo_action_json(request, action='seal_active_segment')


def resolver_excepcion_pago(request):
    access_redirect = _require_admin_dashboard_access(request, allow_get_redirect=True)
    if access_redirect:
        return access_redirect

    if request.method != 'POST':
        return redirect('dashboard_analytics')

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
    access_redirect = _require_admin_dashboard_access(request, allow_get_redirect=True)
    if access_redirect:
        return access_redirect

    if request.method != 'POST':
        return redirect('dashboard_analytics')

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
    access_redirect = _require_admin_dashboard_access(request, allow_get_redirect=True)
    if access_redirect:
        return access_redirect

    if request.method != 'POST':
        return redirect('dashboard_analytics')

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


def _require_admin_dashboard_access(request, *, allow_get_redirect: bool = False):
    if not request.user.is_authenticated:
        return redirect('pos_login')
    if request.user.is_superuser:
        return None
    if hasattr(request.user, 'empleado') and request.user.empleado.rol == 'ADMIN':
        return None
    return redirect('pos_index' if allow_get_redirect or request.method == 'GET' else 'pos_index')


def _require_admin_dashboard_api_access(request):
    if not request.user.is_authenticated:
        return JsonResponse({'detail': 'auth required'}, status=401)
    if request.user.is_superuser:
        return None
    if hasattr(request.user, 'empleado') and request.user.empleado.rol == 'ADMIN':
        return None
    return JsonResponse({'detail': 'admin required'}, status=403)


def _execute_offline_limbo_action_json(request, *, action: str):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    if request.method != 'POST':
        return JsonResponse({'detail': 'method not allowed'}, status=405)
    try:
        return JsonResponse(execute_offline_limbo_action(action=action))
    except OfflineLimboActionError as exc:
        return JsonResponse(
            {
                'detail': str(exc),
                'action': {
                    'name': action,
                    'performed': False,
                },
            },
            status=409,
        )


def _execute_offline_segment_action_json(request, *, action: str):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    if request.method != 'POST':
        return JsonResponse({'detail': 'method not allowed'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'detail': 'json body invalido'}, status=400)
    segment_id = str(body.get('segment_id') or '').strip()
    try:
        return JsonResponse(
            execute_offline_segment_action(
                action=action,
                segment_id=segment_id,
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )
        )
    except OfflineLimboActionError as exc:
        return JsonResponse(
            {
                'detail': str(exc),
                'action': {
                    'name': action,
                    'performed': False,
                },
            },
            status=409,
        )
