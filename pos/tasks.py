from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .delivery_tokens import make_delivery_quote_token
from .models import DeliveryQuote, Empleado, PrintJob, Venta, WhatsAppConversation
from .whatsapp_service import (
    send_whatsapp_confirmation_buttons,
    send_whatsapp_message,
    touch_conversation_outbound,
)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={'max_retries': 3})
def send_delivery_quote_requests(self, venta_id: int):
    venta = Venta.objects.select_related('cliente').get(id=venta_id)
    if venta.tipo_pedido != 'DOMICILIO':
        return

    drivers = Empleado.objects.filter(rol='DELIVERY', activo=True).exclude(telefono='')
    if not drivers.exists():
        return

    if not venta.delivery_quote_deadline_at:
        venta.delivery_quote_deadline_at = timezone.now() + timedelta(
            seconds=settings.DELIVERY_QUOTE_TIMEOUT_SECONDS
        )
        venta.save(update_fields=['delivery_quote_deadline_at'])

    for driver in drivers:
        token = make_delivery_quote_token(venta.id, driver.id)
        link = f"{settings.PUBLIC_BACKEND_URL}/integrations/delivery/quote/{token}/"
        map_url = ''
        if venta.ubicacion_lat is not None and venta.ubicacion_lng is not None:
            map_url = f"https://www.google.com/maps?q={venta.ubicacion_lat},{venta.ubicacion_lng}"

        msg = (
            f"Pedido #{venta.id} para delivery\n"
            f"Cliente: {venta.cliente_nombre}\n"
            f"Total productos: ${venta.total:.2f}\n"
            f"Ubicacion: {map_url or 'No compartida'}\n"
            f"Cotiza aqui: {link}"
        )
        send_whatsapp_message(driver.telefono, msg, raise_on_error=True)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={'max_retries': 3})
def process_delivery_quote_timeout(self, venta_id: int):
    venta = Venta.objects.get(id=venta_id)
    if venta.estado != 'PENDIENTE_COTIZACION':
        return

    if venta.delivery_quote_deadline_at and timezone.now() < venta.delivery_quote_deadline_at:
        return

    customer_phone = venta.telefono_cliente_e164 or venta.telefono_cliente
    if customer_phone:
        send_whatsapp_message(
            customer_phone,
            (
                f"Pedido #{venta.id}: seguimos validando costo de envio. "
                'Nuestro cajero te contactara en breve para resolverlo.'
            ),
            raise_on_error=True,
        )


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={'max_retries': 3})
def notify_customer_quote_total(self, venta_id: int):
    venta = Venta.objects.get(id=venta_id)
    customer_phone = venta.telefono_cliente_e164 or venta.telefono_cliente
    if not customer_phone:
        return

    total_referencial = (venta.total + venta.costo_envio).quantize(Decimal('0.01'))
    send_whatsapp_confirmation_buttons(
        customer_phone,
        venta.id,
        f"{venta.total:.2f}",
        f"{venta.costo_envio:.2f}",
        f"{total_referencial:.2f}",
        raise_on_error=True,
    )

    conv, _ = WhatsAppConversation.objects.get_or_create(
        telefono_e164=customer_phone,
        defaults={'estado_flujo': 'ESPERANDO_CONFIRMACION_TOTAL', 'venta': venta},
    )
    conv.estado_flujo = 'ESPERANDO_CONFIRMACION_TOTAL'
    conv.venta = venta
    conv.save(update_fields=['estado_flujo', 'venta'])
    touch_conversation_outbound(conv)


@shared_task(bind=True)
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


@shared_task(bind=True)
def create_print_jobs(self, venta_id: int):
    venta = Venta.objects.get(id=venta_id)
    PrintJob.objects.get_or_create(venta=venta, tipo='COMANDA', defaults={'estado': 'PENDING'})
    PrintJob.objects.get_or_create(venta=venta, tipo='TICKET', defaults={'estado': 'PENDING'})


@shared_task(bind=True)
def set_quote_and_notify(self, venta_id: int, empleado_id: int, precio: str):
    with transaction.atomic():
        venta = Venta.objects.select_for_update().get(id=venta_id)

        if DeliveryQuote.objects.filter(venta=venta, empleado_delivery_id=empleado_id).exists():
            return {'status': 'duplicate_driver'}

        quote = DeliveryQuote.objects.create(
            venta=venta,
            empleado_delivery_id=empleado_id,
            precio=Decimal(str(precio)),
            estado='PROPUESTA',
        )

        if venta.estado != 'PENDIENTE_COTIZACION':
            quote.estado = 'DESCARTADA'
            quote.save(update_fields=['estado'])
            return {'status': 'late'}

        winner_exists = DeliveryQuote.objects.filter(venta=venta, estado='GANADORA').exclude(id=quote.id).exists()
        if winner_exists:
            quote.estado = 'DESCARTADA'
            quote.save(update_fields=['estado'])
            return {'status': 'ignored'}

        quote.estado = 'GANADORA'
        quote.save(update_fields=['estado'])
        DeliveryQuote.objects.filter(venta=venta, estado='PROPUESTA').exclude(id=quote.id).update(estado='DESCARTADA')

        venta.costo_envio = quote.precio
        venta.estado = 'PENDIENTE'
        venta.confirmacion_cliente = 'PENDIENTE'
        venta.save(update_fields=['costo_envio', 'estado', 'confirmacion_cliente'])

    notify_customer_quote_total.delay(venta_id)
    return {'status': 'ok'}


@shared_task(bind=True)
def sweep_delivery_quote_timeouts(self):
    expired_ids = list(
        Venta.objects.filter(
            estado='PENDIENTE_COTIZACION',
            delivery_quote_deadline_at__isnull=False,
            delivery_quote_deadline_at__lte=timezone.now(),
        )
        .values_list('id', flat=True)[:300]
    )
    for venta_id in expired_ids:
        process_delivery_quote_timeout.delay(venta_id)
    return {'processed': len(expired_ids)}


@shared_task(bind=True)
def requeue_stuck_print_jobs(self):
    threshold = timezone.now() - timedelta(
        seconds=max(30, int(getattr(settings, 'PRINT_JOB_STUCK_SECONDS', 120)))
    )
    updated = PrintJob.objects.filter(
        estado='IN_PROGRESS',
        updated_at__lt=threshold,
    ).update(
        estado='PENDING',
        error='Reencolado automatico: job trabado',
    )
    return {'requeued': int(updated)}
