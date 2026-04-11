from __future__ import annotations

from django.shortcuts import get_object_or_404, render

from pos.models import Venta


def handle_order_confirmation_request(request, pedido_id):
    """Renderiza la confirmacion de un pedido web creado exitosamente."""
    venta = get_object_or_404(Venta, id=pedido_id, origen='WEB')
    return render(
        request,
        'pedidos/confirmacion.html',
        {
            'venta': venta,
            'payment_result': str(request.GET.get('payment_result') or '').strip().lower(),
        },
    )
