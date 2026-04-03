from __future__ import annotations

import json

from django.contrib.auth import login, logout
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie

from pos.application.cash_register import (
    CashRegisterError,
    close_cash_register,
    find_customer_by_identity_document,
    get_cash_closing_context,
    get_cash_opening_context,
    is_valid_identity_document,
    open_cash_register,
    upsert_customer,
    verify_pos_pin,
)
from pos.application.context import resolve_location_for_user


@ensure_csrf_cookie
def pantalla_login(request):
    return render(request, 'pos/login.html')


def verificar_pin(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error'}, status=400)

    data = json.loads(request.body)
    pin = data.get('pin')
    alias = data.get('alias')
    location_uuid = data.get('location_uuid')

    try:
        result = verify_pos_pin(pin, alias=alias, location_uuid=location_uuid)
        login(request, result.user)
        return JsonResponse({
            'status': 'ok',
            'rol': result.rol,
            'empleado_nombre': result.empleado_nombre,
        })
    except CashRegisterError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)


def apertura_caja(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    return render(request, 'pos/apertura.html', get_cash_opening_context(request.user))


def abrir_caja(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'status': 'error'}, status=400)

    data = json.loads(request.body)
    try:
        caja, ya_abierta = open_cash_register(request.user, data.get('monto_inicial', 0))
        return JsonResponse({
            'status': 'ok',
            'ya_abierta': ya_abierta,
            'base_inicial': f'{caja.base_inicial:.2f}',
        })
    except CashRegisterError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)


def cierre_caja(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    context = get_cash_closing_context(request.user)
    if not context:
        return redirect('pos_login')

    return render(request, 'pos/cierre.html', context)


def procesar_cierre(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'status': 'error'}, status=400)

    data = json.loads(request.body)
    try:
        caja = close_cash_register(
            request.user,
            data.get('total_declarado'),
            data.get('conteo'),
            allow_pending_refund_override=bool(data.get('allow_pending_refund_override')),
            pending_refund_override_note=data.get('pending_refund_override_note', ''),
        )
        caja_id = caja.id
        logout(request)
        return JsonResponse({'status': 'ok', 'caja_id': caja_id})
    except CashRegisterError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)


def cerrar_sesion(request):
    logout(request)
    return redirect('pos_login')


def buscar_crear_cliente(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)

    organization = resolve_location_for_user(request.user).organization

    if request.method == 'POST':
        data = json.loads(request.body)
        try:
            cliente = upsert_customer(data, organization=organization)
            return JsonResponse({'status': 'ok', 'cliente_id': cliente.id, 'nombre': cliente.nombre})
        except CashRegisterError as exc:
            return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)

    cedula = request.GET.get('cedula')
    if not cedula:
        return JsonResponse({'status': 'error', 'mensaje': 'Parametro cedula requerido'}, status=400)
    if not is_valid_identity_document(cedula):
        return JsonResponse({'status': 'error', 'mensaje': 'C.I/RUC invalido (10 o 13 digitos)'}, status=400)

    cliente = find_customer_by_identity_document(cedula, organization=organization)
    if not cliente:
        return JsonResponse({'encontrado': False})

    return JsonResponse({
        'encontrado': True,
        'id': cliente.id,
        'nombre': cliente.nombre,
        'direccion': cliente.direccion,
        'telefono': cliente.telefono,
        'email': cliente.email,
    })
