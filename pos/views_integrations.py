from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db.models import Max
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .delivery_tokens import read_delivery_claim_token, read_delivery_quote_token
from .models import DeliveryQuote, Empleado, PrintJob, Venta, WhatsAppConversation, WhatsAppMessageLog
from .tasks import process_customer_confirmation, set_quote_and_notify
from .telegram_service import notify_order_claimed
from .whatsapp_service import (
    extract_inbound_whatsapp,
    send_whatsapp_message,
    touch_conversation_inbound,
    validate_whatsapp_signature,
)
from .whatsapp_utils import normalize_phone_to_e164, parse_customer_confirmation


def _is_whatsapp_rate_limited(phone_e164: str) -> bool:
    key = f'wa:inbound:rl:{phone_e164}'
    window = max(1, int(getattr(settings, 'WHATSAPP_INBOUND_RATE_LIMIT_WINDOW_SECONDS', 60)))
    max_events = max(1, int(getattr(settings, 'WHATSAPP_INBOUND_RATE_LIMIT_MAX', 20)))
    try:
        if cache.add(key, 1, timeout=window):
            return False
        count = cache.incr(key)
        return int(count) > max_events
    except Exception:
        # Fallback defensivo si cache no esta disponible
        recent_count = WhatsAppMessageLog.objects.filter(
            direction='IN',
            telefono_e164=phone_e164,
            created_at__gte=timezone.now() - timedelta(seconds=window),
        ).count()
        return recent_count > max_events


def _webhook_ack_message(phone_e164: str, body: str):
    send_whatsapp_message(phone_e164, body)
    return JsonResponse({'status': 'ok'})


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def whatsapp_webhook(request):
    if request.method == 'GET':
        mode = request.GET.get('hub.mode', '')
        verify_token = request.GET.get('hub.verify_token', '')
        challenge = request.GET.get('hub.challenge', '')
        if mode == 'subscribe' and verify_token and verify_token == settings.META_WHATSAPP_VERIFY_TOKEN:
            return HttpResponse(challenge, status=200)
        return JsonResponse({'status': 'error', 'mensaje': 'invalid verify token'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'method not allowed'}, status=405)

    if not validate_whatsapp_signature(request):
        return JsonResponse({'status': 'error', 'mensaje': 'invalid signature'}, status=403)

    inbound = extract_inbound_whatsapp(request)
    if not inbound:
        return JsonResponse({'status': 'ok', 'mensaje': 'no inbound message'})

    from_raw = inbound.get('from_raw', '')
    body = (inbound.get('body') or '').strip()
    button_text = (inbound.get('button_text') or '').strip()
    button_payload = (inbound.get('button_payload') or '').strip()
    message_sid = inbound.get('message_sid')

    phone_e164 = normalize_phone_to_e164(from_raw)

    if message_sid and WhatsAppMessageLog.objects.filter(message_sid=message_sid).exists():
        return _webhook_ack_message(phone_e164, 'Mensaje ya recibido.')

    if _is_whatsapp_rate_limited(phone_e164):
        WhatsAppMessageLog.objects.create(
            direction='IN',
            telefono_e164=phone_e164,
            payload_json={'reason': 'rate_limited', 'raw': inbound.get('raw_payload') or {}},
            status='rate_limited',
        )
        return _webhook_ack_message(phone_e164, 'Demasiados mensajes en poco tiempo. Intenta en 1 minuto.')

    WhatsAppMessageLog.objects.create(
        direction='IN',
        telefono_e164=phone_e164,
        message_sid=message_sid,
        payload_json=inbound.get('raw_payload') or {},
        status='received',
    )

    conv, _ = WhatsAppConversation.objects.get_or_create(telefono_e164=phone_e164)
    touch_conversation_inbound(conv)

    inbound_text = button_payload or button_text or body
    decision = parse_customer_confirmation(inbound_text)
    if conv.estado_flujo == 'ESPERANDO_CONFIRMACION_TOTAL' and conv.venta_id:
        if decision in {'ACEPTADA', 'RECHAZADA'}:
            process_customer_confirmation.delay(conv.venta_id, decision)
            conv.estado_flujo = 'FINALIZADO'
            conv.save(update_fields=['estado_flujo'])
            return _webhook_ack_message(phone_e164, 'Perfecto. Tu respuesta fue registrada.')

        return _webhook_ack_message(phone_e164, 'Por favor responde SI para confirmar o NO para cancelar.')

    conv.estado_flujo = 'LINK_ENVIADO'
    conv.save(update_fields=['estado_flujo'])

    msg = (
        'Hola. Bienvenido a RAMON by Bosco.\n'
        f'Haz tu pedido aqui: {settings.PUBLIC_PWA_URL}\n\n'
        'Cuando termines te contactaremos por este chat para confirmar envio si aplica.'
    )
    return _webhook_ack_message(phone_e164, msg)


@require_GET
def delivery_quote_form(request, token: str):
    try:
        payload = read_delivery_quote_token(token)
    except Exception:
        return HttpResponse('Token invalido o expirado', status=400)

    venta = Venta.objects.filter(id=payload['venta_id']).first()
    if not venta:
        return HttpResponse('Pedido no encontrado', status=404)

    ya_usado = DeliveryQuote.objects.filter(
        venta_id=payload['venta_id'], empleado_delivery_id=payload['empleado_id']
    ).exists()

    return render(
        request,
        'pos/delivery_quote_form.html',
        {
            'token': token,
            'venta': venta,
            'ya_cotizado': venta.estado != 'PENDIENTE_COTIZACION' or ya_usado,
        },
    )


@csrf_exempt
@require_POST
def delivery_quote_submit(request, token: str):
    try:
        payload = read_delivery_quote_token(token)
    except Exception:
        return JsonResponse({'status': 'error', 'mensaje': 'Token invalido o expirado'}, status=400)

    if DeliveryQuote.objects.filter(
        venta_id=payload['venta_id'], empleado_delivery_id=payload['empleado_id']
    ).exists():
        return JsonResponse({'status': 'ok', 'mensaje': 'Esta cotizacion ya fue enviada'})

    precio_raw = request.POST.get('precio') or request.POST.get('price')
    if precio_raw is None:
        try:
            data = json.loads(request.body or '{}')
            precio_raw = data.get('precio')
        except Exception:
            precio_raw = None

    try:
        precio = Decimal(str(precio_raw)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return JsonResponse({'status': 'error', 'mensaje': 'Precio invalido'}, status=400)

    if precio <= 0:
        return JsonResponse({'status': 'error', 'mensaje': 'Precio debe ser mayor a 0'}, status=400)

    result = set_quote_and_notify.delay(payload['venta_id'], payload['empleado_id'], str(precio))

    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        status = (result.get() or {}).get('status')
        if status in {'duplicate_driver', 'ignored', 'late'}:
            return JsonResponse({'status': 'ok', 'mensaje': 'Cotizacion recibida fuera de ventana'})

    return JsonResponse({'status': 'ok', 'mensaje': 'Cotizacion enviada'})


@csrf_exempt
@require_POST
def confirmar_venta_whatsapp(request, venta_id: int):
    verify_key = request.headers.get('X-Webhook-Verify', '')
    expected = getattr(settings, 'WHATSAPP_WEBHOOK_VERIFY', '')
    if expected and verify_key != expected:
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=401)

    try:
        data = json.loads(request.body or '{}')
    except Exception:
        data = {}

    decision = (data.get('decision') or '').upper().strip()
    if decision not in {'ACEPTADA', 'RECHAZADA'}:
        return JsonResponse({'status': 'error', 'mensaje': 'Decision invalida'}, status=400)

    process_customer_confirmation.delay(venta_id, decision)
    return JsonResponse({'status': 'ok'})


@require_GET
def api_print_jobs_pending(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)

    jobs = PrintJob.objects.select_related('venta').filter(estado='PENDING').order_by('created_at')[:20]

    data = []
    for job in jobs:
        if job.tipo == 'COMANDA':
            print_url = reverse('imprimir_comanda', args=[job.venta_id])
        else:
            print_url = reverse('imprimir_ticket', args=[job.venta_id])
        data.append(
            {
                'id': job.id,
                'venta_id': job.venta_id,
                'tipo': job.tipo,
                'print_url': print_url,
                'created_at': job.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            }
        )

    return JsonResponse({'status': 'ok', 'jobs': data})


@require_GET
def api_print_jobs_failed(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)

    jobs = PrintJob.objects.select_related('venta').filter(estado='FAILED').order_by('-updated_at')[:30]
    data = []
    for job in jobs:
        data.append(
            {
                'id': job.id,
                'venta_id': job.venta_id,
                'tipo': job.tipo,
                'error': job.error,
                'reintentos': job.reintentos,
                'updated_at': job.updated_at.strftime('%Y-%m-%d %H:%M:%S'),
            }
        )
    return JsonResponse({'status': 'ok', 'jobs': data})


@csrf_exempt
@require_POST
def api_print_job_ack(request, job_id: int):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)

    job = PrintJob.objects.filter(id=job_id).first()
    if not job:
        return JsonResponse({'status': 'error', 'mensaje': 'Job no encontrado'}, status=404)

    try:
        data = json.loads(request.body or '{}')
    except Exception:
        data = {}

    done = bool(data.get('done'))
    if done:
        if job.estado not in {'IN_PROGRESS', 'PENDING'}:
            return JsonResponse({'status': 'error', 'mensaje': 'Job en estado invalido'}, status=409)
        job.estado = 'DONE'
        job.save(update_fields=['estado', 'updated_at'])
        return JsonResponse({'status': 'ok', 'estado': job.estado})

    if job.estado != 'PENDING':
        return JsonResponse({'status': 'error', 'mensaje': 'Job ya tomado'}, status=409)

    job.estado = 'IN_PROGRESS'
    job.save(update_fields=['estado', 'updated_at'])
    return JsonResponse({'status': 'ok', 'estado': job.estado})


@csrf_exempt
@require_POST
def api_print_job_fail(request, job_id: int):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)

    job = PrintJob.objects.filter(id=job_id).first()
    if not job:
        return JsonResponse({'status': 'error', 'mensaje': 'Job no encontrado'}, status=404)

    try:
        data = json.loads(request.body or '{}')
    except Exception:
        data = {}

    job.estado = 'FAILED'
    job.reintentos = (job.reintentos or 0) + 1
    job.error = (data.get('error') or 'Fallo de impresion')[:255]
    job.save(update_fields=['estado', 'reintentos', 'error', 'updated_at'])
    return JsonResponse({'status': 'ok', 'estado': job.estado})


@csrf_exempt
@require_POST
def api_print_job_retry(request, job_id: int):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)

    job = PrintJob.objects.filter(id=job_id).first()
    if not job:
        return JsonResponse({'status': 'error', 'mensaje': 'Job no encontrado'}, status=404)

    if job.estado != 'FAILED':
        return JsonResponse({'status': 'error', 'mensaje': 'Solo se puede reintentar un job FAILED'}, status=409)

    job.estado = 'PENDING'
    job.error = ''
    job.save(update_fields=['estado', 'error', 'updated_at'])
    return JsonResponse({'status': 'ok', 'estado': job.estado})


@require_GET
def api_integrations_health(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)

    now = timezone.now()
    stuck_threshold = now - timedelta(
        seconds=max(30, int(getattr(settings, 'PRINT_JOB_STUCK_SECONDS', 120)))
    )

    provider = 'META'
    last_inbound = (
        WhatsAppMessageLog.objects.filter(direction='IN').aggregate(last=Max('created_at')).get('last')
    )
    last_outbound = (
        WhatsAppMessageLog.objects.filter(direction='OUT').aggregate(last=Max('created_at')).get('last')
    )

    timed_out_quotes = Venta.objects.filter(
        estado='PENDIENTE_COTIZACION',
        delivery_quote_deadline_at__isnull=False,
        delivery_quote_deadline_at__lt=now,
    ).count()
    pending_quotes = Venta.objects.filter(estado='PENDIENTE_COTIZACION').count()
    failed_print_jobs = PrintJob.objects.filter(estado='FAILED').count()
    stuck_print_jobs = PrintJob.objects.filter(
        estado='IN_PROGRESS',
        updated_at__lt=stuck_threshold,
    ).count()
    rate_limited_last_hour = WhatsAppMessageLog.objects.filter(
        direction='IN',
        status='rate_limited',
        created_at__gte=now - timedelta(hours=1),
    ).count()

    data = {
        'status': 'ok',
        'whatsapp': {
            'provider': provider,
            'configured': bool(
                settings.META_WHATSAPP_TOKEN
                and settings.META_WHATSAPP_PHONE_NUMBER_ID
                and settings.META_WHATSAPP_VERIFY_TOKEN
            ),
            'signature_validation': bool(settings.META_SIGNATURE_VALIDATION),
            'last_inbound_at': last_inbound.isoformat() if last_inbound else None,
            'last_outbound_at': last_outbound.isoformat() if last_outbound else None,
            'rate_limited_last_hour': rate_limited_last_hour,
        },
        'delivery_quotes': {
            'pending': pending_quotes,
            'timed_out': timed_out_quotes,
        },
        'print_jobs': {
            'failed': failed_print_jobs,
            'stuck_in_progress': stuck_print_jobs,
        },
        'async': {
            'celery_task_always_eager': bool(getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False)),
            'broker_url': getattr(settings, 'CELERY_BROKER_URL', ''),
        },
    }
    return JsonResponse(data)


@require_GET
def delivery_claim_form(request, token: str):
    """GET: show claim page with order details + PIN/price form."""
    try:
        payload = read_delivery_claim_token(token)
    except Exception:
        return render(request, 'pos/delivery_claim.html', {'token_invalido': True, 'venta': None})

    venta = Venta.objects.prefetch_related('detalles__producto').filter(id=payload['venta_id']).first()
    if not venta:
        return HttpResponse('Pedido no encontrado', status=404)

    ya_tomado = venta.repartidor_asignado is not None or venta.estado != 'PENDIENTE_COTIZACION'

    return render(request, 'pos/delivery_claim.html', {
        'token': token,
        'venta': venta,
        'ya_tomado': ya_tomado,
        'token_invalido': False,
    })


@csrf_exempt
@require_POST
def delivery_claim_submit(request, token: str):
    """POST: atomically claim order + set delivery quote."""
    from decimal import Decimal as D
    from django.db import transaction

    try:
        payload = read_delivery_claim_token(token)
    except Exception:
        return render(request, 'pos/delivery_claim.html', {'token_invalido': True, 'venta': None})

    pin = (request.POST.get('pin') or '').strip()
    precio_raw = request.POST.get('precio')

    driver = Empleado.objects.filter(pin=pin, rol='DELIVERY', activo=True).first()
    if not driver:
        venta = Venta.objects.prefetch_related('detalles__producto').filter(id=payload['venta_id']).first()
        return render(request, 'pos/delivery_claim.html', {
            'token': token, 'venta': venta, 'ya_tomado': False,
            'token_invalido': False, 'error': 'PIN invalido o no eres repartidor activo.',
        })

    try:
        precio = D(str(precio_raw)).quantize(D('0.01'))
        if precio <= 0:
            raise ValueError
    except Exception:
        venta = Venta.objects.prefetch_related('detalles__producto').filter(id=payload['venta_id']).first()
        return render(request, 'pos/delivery_claim.html', {
            'token': token, 'venta': venta, 'ya_tomado': False,
            'token_invalido': False, 'error': 'Precio invalido. Debe ser mayor a 0.',
        })

    with transaction.atomic():
        venta = Venta.objects.select_for_update().get(id=payload['venta_id'])

        if venta.repartidor_asignado is not None or venta.estado != 'PENDIENTE_COTIZACION':
            return render(request, 'pos/delivery_claim.html', {
                'token': token, 'venta': venta, 'ya_tomado': True, 'token_invalido': False,
            })

        venta.repartidor_asignado = driver
        venta.save(update_fields=['repartidor_asignado'])

    set_quote_and_notify.delay(venta.id, driver.id, str(precio))

    notify_order_claimed(venta, driver)

    return render(request, 'pos/delivery_claim.html', {
        'token': token, 'venta': venta, 'ya_tomado': True, 'token_invalido': False,
        'claim_exito': True, 'driver_nombre': driver.nombre, 'precio_envio': str(precio),
    })
