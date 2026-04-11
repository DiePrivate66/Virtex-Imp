from __future__ import annotations

import json
from urllib.parse import urlencode

from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from pos.application.web_orders import (
    WebOrderError,
    cancel_payphone_web_order,
    confirm_payphone_web_order,
    get_web_order,
    process_payphone_notification,
)


def handle_payphone_return_request(request):
    pedido_id = request.GET.get('pedido_id')
    try:
        venta = get_web_order(pedido_id)
    except WebOrderError:
        return redirect('/pedido/')

    payphone_id = request.GET.get('id') or request.GET.get('Id') or request.GET.get('paymentId')
    client_transaction_id = (
        request.GET.get('clientTransactionId')
        or request.GET.get('clientTxId')
        or request.GET.get('client_transaction_id')
        or venta.client_transaction_id
    )
    if not payphone_id or not client_transaction_id:
        return _redirect_to_confirmation(venta.id, payment_result='failed')

    try:
        result = confirm_payphone_web_order(
            venta=venta,
            payphone_id=payphone_id,
            client_transaction_id=client_transaction_id,
        )
    except WebOrderError:
        return _redirect_to_confirmation(venta.id, payment_result='failed')

    payment_result = 'paid' if result['status'] == 'paid' else 'failed'
    return _redirect_to_confirmation(venta.id, payment_result=payment_result)


def handle_payphone_cancel_request(request):
    pedido_id = request.GET.get('pedido_id')
    try:
        venta = get_web_order(pedido_id)
    except WebOrderError:
        return redirect('/pedido/')

    try:
        cancel_payphone_web_order(venta, reason='Pago cancelado por el cliente en PayPhone')
    except WebOrderError:
        return _redirect_to_confirmation(venta.id, payment_result='failed')
    return _redirect_to_confirmation(venta.id, payment_result='cancelled')


@csrf_exempt
def handle_payphone_notification_request(request):
    if request.method != 'POST':
        return JsonResponse({'Response': False, 'ErrorCode': '405'}, status=405)
    try:
        payload = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        return JsonResponse({'Response': False, 'ErrorCode': '444'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'Response': False, 'ErrorCode': '444'}, status=400)

    response_payload, status_code = process_payphone_notification(payload)
    return JsonResponse(response_payload, status=status_code)


def _redirect_to_confirmation(pedido_id: int, *, payment_result: str) -> HttpResponseRedirect:
    base_url = reverse('pedido_confirmacion', args=[pedido_id])
    return redirect(f'{base_url}?{urlencode({"payment_result": payment_result})}')
