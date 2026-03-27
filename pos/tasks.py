"""Compatibility facade for Celery tasks.

Historical module: ``pos.tasks``
Canonical target: ``pos.infrastructure.tasks``
Compatibility role: ``operational Celery alias``
Retirement phase: ``phase_6_retire_operational_aliases``
"""

from celery import shared_task

from pos.legacy import build_legacy_module_metadata

(
    LEGACY_MODULE_PATH,
    LEGACY_CONTRACT,
    CANONICAL_TARGET,
    COMPATIBILITY_ROLE,
    REMOVAL_PHASE,
) = build_legacy_module_metadata('pos.tasks')

from pos.infrastructure.tasks import (
    create_print_jobs as _create_print_jobs,
    notify_customer_quote_total as _notify_customer_quote_total,
    process_customer_confirmation as _process_customer_confirmation,
    process_delivery_quote_timeout as _process_delivery_quote_timeout,
    queue_delivery_receipt_ticket as _queue_delivery_receipt_ticket,
    requeue_stuck_print_jobs as _requeue_stuck_print_jobs,
    send_delivery_quote_requests as _send_delivery_quote_requests,
    set_quote_and_notify as _set_quote_and_notify,
    sweep_delivery_quote_timeouts as _sweep_delivery_quote_timeouts,
)


@shared_task(name='pos.tasks.create_print_jobs', bind=True)
def create_print_jobs(self, *args, **kwargs):
    return _create_print_jobs.run(*args, **kwargs)


@shared_task(name='pos.tasks.notify_customer_quote_total', bind=True)
def notify_customer_quote_total(self, *args, **kwargs):
    return _notify_customer_quote_total.run(*args, **kwargs)


@shared_task(name='pos.tasks.process_customer_confirmation', bind=True)
def process_customer_confirmation(self, *args, **kwargs):
    return _process_customer_confirmation.run(*args, **kwargs)


@shared_task(name='pos.tasks.process_delivery_quote_timeout', bind=True)
def process_delivery_quote_timeout(self, *args, **kwargs):
    return _process_delivery_quote_timeout.run(*args, **kwargs)


@shared_task(name='pos.tasks.queue_delivery_receipt_ticket', bind=True)
def queue_delivery_receipt_ticket(self, *args, **kwargs):
    return _queue_delivery_receipt_ticket.run(*args, **kwargs)


@shared_task(name='pos.tasks.requeue_stuck_print_jobs', bind=True)
def requeue_stuck_print_jobs(self, *args, **kwargs):
    return _requeue_stuck_print_jobs.run(*args, **kwargs)


@shared_task(name='pos.tasks.send_delivery_quote_requests', bind=True)
def send_delivery_quote_requests(self, *args, **kwargs):
    return _send_delivery_quote_requests.run(*args, **kwargs)


@shared_task(name='pos.tasks.set_quote_and_notify', bind=True)
def set_quote_and_notify(self, *args, **kwargs):
    return _set_quote_and_notify.run(*args, **kwargs)


@shared_task(name='pos.tasks.sweep_delivery_quote_timeouts', bind=True)
def sweep_delivery_quote_timeouts(self, *args, **kwargs):
    return _sweep_delivery_quote_timeouts.run(*args, **kwargs)

__all__ = [
    'CANONICAL_TARGET',
    'COMPATIBILITY_ROLE',
    'LEGACY_CONTRACT',
    'LEGACY_MODULE_PATH',
    'REMOVAL_PHASE',
    'create_print_jobs',
    'notify_customer_quote_total',
    'process_customer_confirmation',
    'process_delivery_quote_timeout',
    'queue_delivery_receipt_ticket',
    'requeue_stuck_print_jobs',
    'send_delivery_quote_requests',
    'set_quote_and_notify',
    'sweep_delivery_quote_timeouts',
]
