from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from pos.models import PrintJob, Venta


@shared_task(name='pos.infrastructure.tasks.create_print_jobs', bind=True)
def create_print_jobs(self, venta_id: int):
    venta = Venta.objects.get(id=venta_id)
    PrintJob.objects.get_or_create(venta=venta, tipo='COMANDA', defaults={'estado': 'PENDING'})
    PrintJob.objects.get_or_create(venta=venta, tipo='TICKET', defaults={'estado': 'PENDING'})


@shared_task(name='pos.infrastructure.tasks.requeue_stuck_print_jobs', bind=True)
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
