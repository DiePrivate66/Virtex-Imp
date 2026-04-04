from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from pos.infrastructure.offline import (
    OfflineJournalEnvelope,
    OfflineJournalRuntimeConfig,
    append_offline_journal_envelope,
)
from pos.models import Venta


logger = logging.getLogger(__name__)


def capture_paid_sale_to_offline_journal(
    *,
    venta_id: int,
    capture_event_type: str,
    capture_source: str = 'server_django_sales',
) -> None:
    if not _offline_capture_enabled():
        return
    venta = (
        Venta.objects.select_related('organization', 'location', 'operator')
        .filter(id=venta_id)
        .first()
    )
    if not venta:
        return

    try:
        append_offline_journal_envelope(
            config=_build_runtime_config(),
            envelope=OfflineJournalEnvelope(
                event_id=_build_event_id(venta=venta, capture_event_type=capture_event_type),
                journal_event_type='sale',
                payload=_build_sale_payload(
                    venta=venta,
                    capture_event_type=capture_event_type,
                    capture_source=capture_source,
                ),
                client_transaction_id=venta.client_transaction_id,
                queue_session_id=venta.queue_session_id,
                session_seq_no=venta.session_seq_no,
                client_created_at_raw=venta.client_created_at_raw,
                client_monotonic_ms=venta.client_monotonic_ms,
            ),
        )
    except Exception:
        logger.exception('No se pudo capturar venta pagada #%s en offline journal', venta_id)


def capture_sale_lifecycle_to_offline_journal(
    *,
    venta_id: int,
    capture_event_type: str,
    reason: str = '',
    capture_source: str = 'server_django_sales',
) -> None:
    if not _offline_capture_enabled():
        return
    venta = (
        Venta.objects.select_related('organization', 'location', 'operator')
        .filter(id=venta_id)
        .first()
    )
    if not venta:
        return

    try:
        payload = _build_sale_payload(
            venta=venta,
            capture_event_type=capture_event_type,
            capture_source=capture_source,
        )
        if reason:
            payload['failure_reason'] = reason[:255]
        append_offline_journal_envelope(
            config=_build_runtime_config(),
            envelope=OfflineJournalEnvelope(
                event_id=_build_event_id(venta=venta, capture_event_type=capture_event_type),
                journal_event_type='lifecycle',
                payload=payload,
                client_transaction_id=venta.client_transaction_id,
                queue_session_id=venta.queue_session_id,
                session_seq_no=venta.session_seq_no,
                client_created_at_raw=venta.client_created_at_raw,
                client_monotonic_ms=venta.client_monotonic_ms,
            ),
        )
    except Exception:
        logger.exception('No se pudo capturar lifecycle de venta #%s en offline journal', venta_id)


def _offline_capture_enabled() -> bool:
    return bool(
        getattr(settings, 'OFFLINE_JOURNAL_ENABLED', False)
        and getattr(settings, 'OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS', False)
        and str(getattr(settings, 'OFFLINE_JOURNAL_ROOT', '') or '').strip()
    )


def _build_runtime_config() -> OfflineJournalRuntimeConfig:
    return OfflineJournalRuntimeConfig(
        root_dir=Path(getattr(settings, 'OFFLINE_JOURNAL_ROOT', '')),
        stream_name=getattr(settings, 'OFFLINE_JOURNAL_STREAM_NAME', 'sales'),
        segment_max_bytes=getattr(settings, 'OFFLINE_JOURNAL_SEGMENT_MAX_BYTES', 100 * 1024 * 1024),
        limbo_recent_limit=getattr(settings, 'OFFLINE_JOURNAL_LIMBO_RECENT_LIMIT', 50),
    )


def _build_event_id(*, venta: Venta, capture_event_type: str) -> str:
    effective_timestamp = (
        venta.payment_checked_at
        or venta.accounting_booked_at
        or venta.operated_at_normalized
        or venta.fecha
        or timezone.now()
    )
    return (
        f'{capture_event_type}-{venta.id}-{int(effective_timestamp.timestamp() * 1000000)}'
    )[:120]


def _build_sale_payload(*, venta: Venta, capture_event_type: str, capture_source: str) -> dict:
    return {
        'journal_capture_source': capture_source,
        'capture_event_type': capture_event_type,
        'sale_id': venta.id,
        'sale_origin': venta.origen,
        'organization_id': venta.organization_id,
        'organization_slug': getattr(venta.organization, 'slug', ''),
        'location_id': venta.location_id,
        'location_uuid': str(getattr(venta.location, 'uuid', '') or ''),
        'sale_total': f'{venta.total:.2f}',
        'payment_status': venta.payment_status,
        'payment_reference': venta.payment_reference,
        'payment_provider': venta.payment_provider,
        'payment_method_type': venta.payment_method_type,
        'metodo_pago': venta.metodo_pago,
        'estado': venta.estado,
        'client_transaction_id': venta.client_transaction_id,
        'display_name': venta.cliente_nombre or '',
        'chronology_estimated': bool(venta.chronology_estimated),
        'operated_at_normalized': venta.operated_at_normalized.isoformat() if venta.operated_at_normalized else '',
        'accounting_booked_at': venta.accounting_booked_at.isoformat() if venta.accounting_booked_at else '',
    }
