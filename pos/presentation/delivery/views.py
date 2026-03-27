from __future__ import annotations

import json
import logging

from django.http import HttpResponse
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from pos.application.delivery import (
    claim_delivery_order,
    confirm_delivery_completed,
    DeliveryError,
    get_delivery_claim_form_context,
    get_delivery_delivered_form_context,
    get_delivery_in_transit_form_context,
    get_delivery_quote_form_context,
    get_manual_delivery_portal_context,
    mark_customer_received,
    mark_delivery_in_transit,
    register_delivery_and_claim_order,
    submit_manual_delivery_quote,
    submit_tokenized_delivery_quote,
)

logger = logging.getLogger(__name__)


def delivery_portal(request):
    return render(request, 'pos/delivery_portal.html', get_manual_delivery_portal_context())


@csrf_exempt
def api_fijar_precio(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Metodo no permitido'})

    try:
        data = json.loads(request.body)
        submit_manual_delivery_quote(
            pedido_id=data.get('pedido_id'),
            precio=data.get('precio', 0),
            user=request.user,
        )
        return JsonResponse({'status': 'ok'})
    except DeliveryError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)
    except Exception:
        logger.exception('Error inesperado fijando precio de delivery')
        return JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo fijar el precio del delivery. Intenta nuevamente.'},
            status=500,
        )


@require_GET
def delivery_quote_form(request, token: str):
    try:
        context = get_delivery_quote_form_context(token)
    except DeliveryError as exc:
        return HttpResponse(exc.message, status=exc.status_code)
    return render(request, 'pos/delivery_quote_form.html', context)


@csrf_exempt
@require_POST
def delivery_quote_submit(request, token: str):
    precio_raw = request.POST.get('precio') or request.POST.get('price')
    if precio_raw is None:
        try:
            data = json.loads(request.body or '{}')
            precio_raw = data.get('precio')
        except Exception:
            precio_raw = None

    try:
        submit_tokenized_delivery_quote(token=token, precio=precio_raw)
        return JsonResponse({'status': 'ok', 'mensaje': 'Cotizacion enviada'})
    except DeliveryError as exc:
        if exc.message == 'Esta cotizacion ya fue enviada':
            return JsonResponse({'status': 'ok', 'mensaje': exc.message})
        if exc.message == 'Cotizacion recibida fuera de ventana':
            return JsonResponse({'status': 'ok', 'mensaje': exc.message})
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)


@require_GET
def delivery_claim_form(request, token: str):
    try:
        context = get_delivery_claim_form_context(token)
    except DeliveryError as exc:
        return HttpResponse(exc.message, status=exc.status_code)
    return render(request, 'pos/delivery_claim.html', context)


@csrf_exempt
@require_POST
def delivery_claim_submit(request, token: str):
    try:
        base_context = get_delivery_claim_form_context(token)
    except DeliveryError as exc:
        return HttpResponse(exc.message, status=exc.status_code)

    if base_context.get('token_invalido'):
        return render(request, 'pos/delivery_claim.html', base_context)

    pin = (request.POST.get('pin') or '').strip()
    precio_raw = request.POST.get('precio')
    flow = (request.POST.get('flow') or 'claim').strip()

    try:
        if flow == 'register':
            claim = register_delivery_and_claim_order(
                token=token,
                nombre=request.POST.get('nombre'),
                telefono=request.POST.get('telefono'),
                pin=request.POST.get('nuevo_pin'),
                precio=precio_raw,
            )
        else:
            claim = claim_delivery_order(token=token, pin=pin, precio=precio_raw)
    except DeliveryError as exc:
        if exc.message == 'Pedido ya tomado':
            context = {**base_context, 'ya_tomado': True}
            return render(request, 'pos/delivery_claim.html', context)
        context = {
            **base_context,
            'error': exc.message,
            'ya_tomado': False,
            'registration_mode': flow == 'register',
            'form_nombre': (request.POST.get('nombre') or '').strip(),
            'form_telefono': (request.POST.get('telefono') or '').strip(),
            'form_precio': (precio_raw or '').strip() if hasattr(precio_raw, 'strip') else precio_raw,
        }
        return render(request, 'pos/delivery_claim.html', context)

    updated_context = get_delivery_claim_form_context(token)
    context = {
        **updated_context,
        'claim_exito': True,
        'driver_nombre': claim.empleado_nombre,
        'precio_envio': claim.precio,
    }
    return render(request, 'pos/delivery_claim.html', context)


@require_GET
def delivery_in_transit_form(request, token: str):
    try:
        context = get_delivery_in_transit_form_context(token)
    except DeliveryError as exc:
        return HttpResponse(exc.message, status=exc.status_code)
    return render(request, 'pos/delivery_in_transit.html', context)


@csrf_exempt
@require_POST
def delivery_in_transit_submit(request, token: str):
    try:
        base_context = get_delivery_in_transit_form_context(token)
    except DeliveryError as exc:
        return HttpResponse(exc.message, status=exc.status_code)

    if base_context.get('token_invalido'):
        return render(request, 'pos/delivery_in_transit.html', base_context)

    pin = (request.POST.get('pin') or '').strip()
    eta_raw = request.POST.get('eta_minutos') or request.POST.get('eta')

    try:
        submission = mark_delivery_in_transit(token=token, pin=pin, eta_minutos=eta_raw)
    except DeliveryError as exc:
        context = {**base_context, 'error': exc.message}
        return render(request, 'pos/delivery_in_transit.html', context)

    updated_context = get_delivery_in_transit_form_context(token)
    context = {
        **updated_context,
        'success': True,
        'driver_nombre': submission.empleado_nombre,
        'eta_minutos': submission.eta_minutos,
    }
    return render(request, 'pos/delivery_in_transit.html', context)


@require_GET
def delivery_delivered_form(request, token: str):
    try:
        context = get_delivery_delivered_form_context(token)
    except DeliveryError as exc:
        return HttpResponse(exc.message, status=exc.status_code)
    return render(request, 'pos/delivery_delivered.html', context)


@csrf_exempt
@require_POST
def delivery_delivered_submit(request, token: str):
    try:
        base_context = get_delivery_delivered_form_context(token)
    except DeliveryError as exc:
        return HttpResponse(exc.message, status=exc.status_code)

    if base_context.get('token_invalido'):
        return render(request, 'pos/delivery_delivered.html', base_context)

    pin = (request.POST.get('pin') or '').strip()

    try:
        submission = confirm_delivery_completed(token=token, pin=pin)
    except DeliveryError as exc:
        context = {**base_context, 'error': exc.message}
        return render(request, 'pos/delivery_delivered.html', context)

    updated_context = get_delivery_delivered_form_context(token)
    context = {
        **updated_context,
        'success': True,
        'driver_nombre': submission.empleado_nombre,
    }
    return render(request, 'pos/delivery_delivered.html', context)
