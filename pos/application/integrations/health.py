from __future__ import annotations

from datetime import timedelta
import re

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from pos.models import PrintJob, Venta, WhatsAppMessageLog


def get_integrations_health_payload() -> dict:
    now = timezone.now()
    signature_validation_enabled = bool(settings.META_SIGNATURE_VALIDATION)
    token_configured = bool(getattr(settings, 'META_WHATSAPP_TOKEN', ''))
    phone_number_id = str(getattr(settings, 'META_WHATSAPP_PHONE_NUMBER_ID', '') or '').strip().lstrip('=').strip()
    phone_number_id_configured = bool(phone_number_id)
    phone_number_id_valid = bool(re.fullmatch(r'\d+', phone_number_id))
    verify_token_configured = bool(getattr(settings, 'META_WHATSAPP_VERIFY_TOKEN', ''))
    app_secret_configured = bool(getattr(settings, 'META_WHATSAPP_APP_SECRET', ''))
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
                token_configured
                and phone_number_id_configured
                and phone_number_id_valid
                and verify_token_configured
                and (not signature_validation_enabled or app_secret_configured)
            ),
            'signature_validation': signature_validation_enabled,
            'token_configured': token_configured,
            'phone_number_id_configured': phone_number_id_configured,
            'phone_number_id_valid': phone_number_id_valid,
            'verify_token_configured': verify_token_configured,
            'app_secret_configured': app_secret_configured,
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
