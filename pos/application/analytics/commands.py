from __future__ import annotations

from pathlib import Path

from django.conf import settings

from pos.infrastructure.offline import (
    JournalIntegrityError,
    reconcile_snapshot_with_segment,
    reseal_segment_from_snapshot,
)
from pos.infrastructure.offline.writer import journal_runtime_lock

from .queries import build_offline_limbo_payload


class OfflineLimboActionError(RuntimeError):
    pass


def execute_offline_limbo_action(*, action: str) -> dict:
    action_name = str(action or '').strip().lower()
    if action_name not in {'reconcile_sidecar', 'reseal_segment'}:
        raise OfflineLimboActionError('offline limbo action desconocida')

    enabled = bool(getattr(settings, 'OFFLINE_JOURNAL_ENABLED', False))
    if not enabled:
        raise OfflineLimboActionError('runtime offline desactivado')

    root_value = str(getattr(settings, 'OFFLINE_JOURNAL_ROOT', '') or '').strip()
    if not root_value:
        raise OfflineLimboActionError('OFFLINE_JOURNAL_ROOT no esta configurado')

    root_dir = Path(root_value)
    if not root_dir.exists() or not root_dir.is_dir():
        raise OfflineLimboActionError(f'root offline invalido: {root_dir}')

    stream_name = getattr(settings, 'OFFLINE_JOURNAL_STREAM_NAME', 'sales')
    try:
        with journal_runtime_lock(root_dir, stream_name):
            payload = build_offline_limbo_payload()
            limbo = payload.get('limbo') or {}
            segment_path_value = str(limbo.get('segment_path') or '').strip()
            snapshot_path_value = str(limbo.get('snapshot_path') or '').strip()
            segment_id = str(limbo.get('segment_id') or '').strip()

            if not segment_path_value or not snapshot_path_value or not segment_id:
                raise OfflineLimboActionError('no hay segmento activo para operar')

            segment_path = Path(segment_path_value)
            snapshot_path = Path(snapshot_path_value)

            if action_name == 'reconcile_sidecar':
                snapshot = reconcile_snapshot_with_segment(
                    segment_path,
                    snapshot_path,
                    segment_id=segment_id,
                )
                performed = True
                detail = (
                    'Sidecar reconciliado con el journal activo. '
                    f'{int(snapshot.get("record_count") or 0)} records confirmados.'
                )
            else:
                resealed = reseal_segment_from_snapshot(segment_path, snapshot_path)
                performed = bool(resealed)
                if resealed:
                    detail = 'Footer del segmento re-sellado desde el sidecar.'
                else:
                    detail = 'El segmento ya estaba sellado o no tenia footer pendiente.'
            refreshed = build_offline_limbo_payload()
    except JournalIntegrityError as exc:
        raise OfflineLimboActionError(str(exc)) from exc

    refreshed['action'] = {
        'name': action_name,
        'performed': performed,
        'detail': detail,
    }
    return refreshed
