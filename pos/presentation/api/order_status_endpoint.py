from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from pos.models import Venta


def handle_order_status_request(request, pedido_id):
    venta = get_object_or_404(
        Venta.objects.select_related('repartidor_asignado'),
        id=pedido_id,
        origen='WEB',
    )
    return JsonResponse(
        {
            'pedido_id': venta.id,
            'estado': venta.estado,
            'estado_display': venta.get_estado_display(),
            'tipo_pedido': venta.tipo_pedido,
            'metodo_pago': venta.metodo_pago,
            'payment_status': venta.payment_status,
            'payment_status_display': venta.get_payment_status_display(),
            'payment_provider': venta.payment_provider,
            'payment_reference': venta.payment_reference,
            'payment_failure_reason': venta.payment_failure_reason,
            'cliente_nombre': venta.cliente_nombre,
            'telefono_cliente': venta.telefono_cliente,
            'total': f'{venta.total:.2f}',
            'costo_envio': f'{venta.costo_envio:.2f}',
            'envio_pago_repartidor': f'{venta.costo_envio:.2f}',
            'total_a_cobrar_local': f'{venta.total:.2f}',
            # Legacy clients used this field as the customer-facing amount to pay.
            # Delivery is now paid directly to the driver, so keep it product-only.
            'total_con_envio': f'{venta.total:.2f}',
            'total_referencial_con_envio': f'{venta.total_con_envio:.2f}',
            'tiempo_estimado_minutos': venta.tiempo_estimado_minutos,
            'minutos_restantes_estimados': venta.minutos_restantes_estimados,
            'repartidor_nombre': venta.repartidor_asignado.nombre if venta.repartidor_asignado else '',
            'cliente_reporto_recibido': venta.cliente_reporto_recibido_at is not None,
            'repartidor_confirmo_entrega': venta.repartidor_confirmo_entrega_at is not None,
            'puede_reportar_recibido': (
                venta.tipo_pedido == 'DOMICILIO'
                and venta.estado == 'EN_CAMINO'
                and venta.cliente_reporto_recibido_at is None
            ),
            'esperando_confirmacion_delivery': (
                venta.cliente_reporto_recibido_at is not None
                and venta.repartidor_confirmo_entrega_at is None
            ),
            'payphone_checkout_pending': (
                venta.metodo_pago == 'PAYPHONE' and venta.payment_status == Venta.PaymentStatus.PENDING
            ),
        }
    )
