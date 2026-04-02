from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import connection
from django.db.models import F
from django.db.models.expressions import RawSQL
from django.utils import timezone

from pos.application.notifications import notify_admin_exception_alert
from pos.models import OutboxEvent, PrintJob, Venta


def _claim_outbox_event(event_id: int, *, now=None):
    claim_time = now or timezone.now()
    updated = OutboxEvent.objects.filter(
        id=event_id,
        status__in=[OutboxEvent.Status.PENDING, OutboxEvent.Status.FAILED],
        available_at__lte=claim_time,
    ).update(
        status=OutboxEvent.Status.IN_PROGRESS,
        attempts=F('attempts') + 1,
        last_error='',
        updated_at=claim_time,
    )
    if not updated:
        current = (
            OutboxEvent.objects.filter(id=event_id)
            .values('status', 'available_at', 'attempts')
            .first()
        )
        return None, current
    return OutboxEvent.objects.get(id=event_id), None


def _retry_delay_for_event(event: OutboxEvent) -> int:
    base_delay = 15 if event.priority == OutboxEvent.Priority.CRITICAL else 30
    return min(300, max(base_delay, event.attempts * base_delay))


def _dispatch_score(event: OutboxEvent, *, now) -> tuple[int, object, object, int]:
    aging_step_seconds = max(60, int(getattr(settings, 'OUTBOX_PRIORITY_AGING_STEP_SECONDS', 300)))
    aging_factor = max(1, int(getattr(settings, 'OUTBOX_PRIORITY_AGING_FACTOR', 5)))
    wait_seconds = max(0, int((now - event.created_at).total_seconds()))
    aging_buckets = wait_seconds // aging_step_seconds
    effective_priority = max(1, int(event.priority) - (aging_buckets * aging_factor))
    return (effective_priority, event.available_at, event.created_at, event.id)


def _outbox_priority_score_sql():
    aging_step_seconds = max(60, int(getattr(settings, 'OUTBOX_PRIORITY_AGING_STEP_SECONDS', 300)))
    aging_factor = max(1, int(getattr(settings, 'OUTBOX_PRIORITY_AGING_FACTOR', 5)))

    if connection.vendor == 'postgresql':
        sql = """
            (
                priority
                - (
                    FLOOR(EXTRACT(EPOCH FROM (NOW() - created_at)) / %s) * %s
                )
            )
        """
    else:
        sql = """
            (
                priority
                - (
                    CAST((((julianday('now') - julianday(created_at)) * 86400.0) / %s) AS INTEGER) * %s
                )
            )
        """
    return RawSQL(sql, (aging_step_seconds, aging_factor))


def _dispatch_outbox_event(event: OutboxEvent):
    if event.event_type == 'SALE_PAID_PRINT':
        venta_id = event.payload_json.get('venta_id') or event.aggregate_id
        venta = Venta.objects.get(id=venta_id)
        print_types = event.payload_json.get('print_types') or ['COMANDA', 'TICKET']
        for print_type in print_types:
            PrintJob.objects.get_or_create(
                venta=venta,
                tipo=print_type,
                defaults={
                    'estado': 'PENDING',
                    'organization': venta.organization,
                    'location': venta.location,
                    'correlation_id': event.correlation_id,
                },
            )
        return {'venta_id': venta.id, 'created_print_jobs': len(print_types)}

    if event.event_type == 'ADMIN_EXCEPTION_ALERT':
        sent = notify_admin_exception_alert(event.payload_json or {})
        if not sent:
            raise RuntimeError('No se pudo entregar la alerta critica al canal administrativo')
        return {'sent': bool(sent), 'alert_type': event.payload_json.get('alert_type', 'UNKNOWN')}

    raise ValueError(f'Unsupported outbox event type: {event.event_type}')


@shared_task(name='pos.infrastructure.tasks.process_outbox_event', bind=True)
def process_outbox_event(self, event_id: int):
    event, current = _claim_outbox_event(event_id, now=timezone.now())
    if not event:
        return {'status': 'skipped', 'event_id': event_id, 'current': current or {}}

    try:
        result = _dispatch_outbox_event(event)
    except Exception as exc:
        retry_delay = _retry_delay_for_event(event)
        next_status = (
            OutboxEvent.Status.BLOCKED
            if event.priority == OutboxEvent.Priority.CRITICAL and event.attempts >= 10
            else OutboxEvent.Status.FAILED
        )
        OutboxEvent.objects.filter(id=event.id).update(
            status=next_status,
            last_error=str(exc)[:255],
            available_at=timezone.now() + timedelta(seconds=retry_delay),
        )
        raise

    OutboxEvent.objects.filter(id=event.id).update(
        status=OutboxEvent.Status.DONE,
        last_error='',
        available_at=timezone.now(),
    )
    return {'status': 'done', 'event_id': event.id, 'result': result}


@shared_task(name='pos.infrastructure.tasks.sweep_stale_outbox_events', bind=True)
def sweep_stale_outbox_events(self):
    threshold_seconds = max(60, int(getattr(settings, 'OUTBOX_STALE_SECONDS', 300)))
    enqueue_limit = max(1, int(getattr(settings, 'OUTBOX_SWEEP_BATCH_SIZE', 200)))
    now = timezone.now()
    threshold = now - timedelta(seconds=threshold_seconds)
    stale_in_progress = list(
        OutboxEvent.objects.filter(
            status=OutboxEvent.Status.IN_PROGRESS,
            updated_at__lte=threshold,
        )
        .values_list('id', flat=True)[:200]
    )
    if stale_in_progress:
        OutboxEvent.objects.filter(id__in=stale_in_progress).update(
            status=OutboxEvent.Status.FAILED,
            last_error='Reencolado automatico: evento trabado',
            available_at=now,
        )

    candidate_ids = list(
        OutboxEvent.objects.filter(
            status__in=[OutboxEvent.Status.PENDING, OutboxEvent.Status.FAILED],
            available_at__lte=now,
            updated_at__lte=threshold,
        )
        .annotate(priority_score=_outbox_priority_score_sql())
        .order_by('priority_score', 'available_at', 'created_at', 'id')
        .values_list('id', flat=True)[:enqueue_limit]
    )
    for event_id in candidate_ids:
        process_outbox_event.delay(event_id)
    return {'reenqueued': len(candidate_ids)}
