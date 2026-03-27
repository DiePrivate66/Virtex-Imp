from __future__ import annotations

from celery import shared_task
from django.utils import timezone

from pos.infrastructure.notifications import send_whatsapp_message
from pos.models import Venta
from pos.infrastructure.tasks.printing import create_print_jobs


@shared_task(name='pos.infrastructure.tasks.process_customer_confirmation', bind=True)
def process_customer_confirmation(self, venta_id: int, decision: str):
    venta = Venta.objects.get(id=venta_id)
    customer_phone = venta.telefono_cliente_e164 or venta.telefono_cliente

    if decision == 'ACEPTADA':
        venta.confirmacion_cliente = 'ACEPTADA'
        venta.confirmada_por_bot_at = timezone.now()
        venta.estado = 'COCINA'
        venta.save(update_fields=['confirmacion_cliente', 'confirmada_por_bot_at', 'estado'])
        create_print_jobs.delay(venta.id)
        if customer_phone:
            send_whatsapp_message(
                customer_phone,
                f'Pedido #{venta.id} confirmado. Ya enviamos tu orden a cocina.',
                raise_on_error=True,
            )
        if venta.repartidor_asignado and venta.repartidor_asignado.telefono:
            send_whatsapp_message(
                venta.repartidor_asignado.telefono,
                f'Pedido #{venta.id} CONFIRMADO por el cliente. Preparate para recogerlo.',
                raise_on_error=False,
            )
    else:
        venta.confirmacion_cliente = 'RECHAZADA'
        venta.estado = 'CANCELADO'
        venta.save(update_fields=['confirmacion_cliente', 'estado'])
        if customer_phone:
            send_whatsapp_message(
                customer_phone,
                f'Pedido #{venta.id} cancelado. Si deseas, puedes crear uno nuevo desde la PWA.',
                raise_on_error=True,
            )
