from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from pos.models import PrintJob, Venta, WhatsAppMessageLog


def get_integrations_health_payload() -> dict:
    now = timezone.now()
    stuck_threshold = now - timedelta(
        seconds=max(30, int(getattr(settings, 'PRINT_JOB_STUCK_SECONDS', 120)))
    )

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

    return {
        'status': 'ok',
        'whatsapp': {
            'provider': 'META',
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
