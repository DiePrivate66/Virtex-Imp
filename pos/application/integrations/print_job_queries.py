from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

from pos.models import PrintJob


@dataclass(frozen=True)
class PrintJobListItem:
    id: int
    venta_id: int
    tipo: str
    created_at: str | None = None
    updated_at: str | None = None
    print_url: str | None = None
    error: str = ''
    reintentos: int = 0

    def to_pending_payload(self) -> dict:
        return {
            'id': self.id,
            'venta_id': self.venta_id,
            'tipo': self.tipo,
            'print_url': self.print_url,
            'created_at': self.created_at,
        }

    def to_failed_payload(self) -> dict:
        return {
            'id': self.id,
            'venta_id': self.venta_id,
            'tipo': self.tipo,
            'error': self.error,
            'reintentos': self.reintentos,
            'updated_at': self.updated_at,
        }


def get_pending_print_jobs(limit: int = 20) -> list[dict]:
    jobs = (
        PrintJob.objects.select_related('venta')
        .filter(estado='PENDING')
        .order_by('created_at')[:limit]
    )
    data: list[dict] = []
    for job in jobs:
        item = PrintJobListItem(
            id=job.id,
            venta_id=job.venta_id,
            tipo=job.tipo,
            print_url=_get_print_url(job),
            created_at=job.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        )
        data.append(item.to_pending_payload())
    return data


def get_failed_print_jobs(limit: int = 30) -> list[dict]:
    jobs = (
        PrintJob.objects.select_related('venta')
        .filter(estado='FAILED')
        .order_by('-updated_at')[:limit]
    )
    data: list[dict] = []
    for job in jobs:
        item = PrintJobListItem(
            id=job.id,
            venta_id=job.venta_id,
            tipo=job.tipo,
            error=job.error,
            reintentos=job.reintentos,
            updated_at=job.updated_at.strftime('%Y-%m-%d %H:%M:%S'),
        )
        data.append(item.to_failed_payload())
    return data


def _get_print_url(job: PrintJob) -> str:
    if job.tipo == 'COMANDA':
        return reverse('imprimir_comanda', args=[job.venta_id])
    return reverse('imprimir_ticket', args=[job.venta_id])
