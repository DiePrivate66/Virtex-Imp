from __future__ import annotations

from celery import shared_task


@shared_task(name='pos.infrastructure.tasks.reap_stale_pending_sales', bind=True)
def reap_stale_pending_sales(self):
    from pos.application.sales.commands import expire_stale_pending_sales

    return expire_stale_pending_sales()


@shared_task(name='pos.infrastructure.tasks.purge_expired_idempotency_records', bind=True)
def purge_expired_idempotency_records(self):
    from pos.application.sales.commands import purge_expired_idempotency_records

    return purge_expired_idempotency_records()
