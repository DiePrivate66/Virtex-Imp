from __future__ import annotations

from pos.models import PrintJob

from .errors import IntegrationsError


def acknowledge_print_job(job_id: int, *, done: bool = False) -> PrintJob:
    job = _get_job(job_id)

    if done:
        if job.estado not in {'IN_PROGRESS', 'PENDING'}:
            raise IntegrationsError('Job en estado invalido', status_code=409)
        job.estado = 'DONE'
        job.save(update_fields=['estado', 'updated_at'])
        return job

    if job.estado != 'PENDING':
        raise IntegrationsError('Job ya tomado', status_code=409)

    job.estado = 'IN_PROGRESS'
    job.save(update_fields=['estado', 'updated_at'])
    return job


def fail_print_job(job_id: int, error_message: str | None) -> PrintJob:
    job = _get_job(job_id)
    job.estado = 'FAILED'
    job.reintentos = (job.reintentos or 0) + 1
    job.error = (error_message or 'Fallo de impresion')[:255]
    job.save(update_fields=['estado', 'reintentos', 'error', 'updated_at'])
    return job


def retry_print_job(job_id: int) -> PrintJob:
    job = _get_job(job_id)
    if job.estado != 'FAILED':
        raise IntegrationsError('Solo se puede reintentar un job FAILED', status_code=409)

    job.estado = 'PENDING'
    job.error = ''
    job.save(update_fields=['estado', 'error', 'updated_at'])
    return job


def _get_job(job_id: int) -> PrintJob:
    job = PrintJob.objects.filter(id=job_id).first()
    if not job:
        raise IntegrationsError('Job no encontrado', status_code=404)
    return job
