from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from pos.application.delivery import DeliveryError, mark_customer_received


@csrf_exempt
@require_POST
def handle_order_received_request(request, pedido_id):
    try:
        venta = mark_customer_received(pedido_id=pedido_id)
    except DeliveryError as exc:
        return JsonResponse({'status': 'error', 'mensaje': exc.message}, status=exc.status_code)

    return JsonResponse(
        {
            'status': 'ok',
            'pedido_id': venta.id,
            'mensaje': 'Avisamos a tu repartidor para confirmar la entrega final.',
        }
    )
