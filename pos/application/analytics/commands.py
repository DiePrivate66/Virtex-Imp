from __future__ import annotations

from pathlib import Path

from django.conf import settings

from pos.infrastructure.offline import (
    JournalIntegrityError,
    OfflineJournalRuntimeConfig,
    SegmentedJournalRuntime,
    reconcile_snapshot_with_segment,
    reseal_segment_from_snapshot,
)
from pos.infrastructure.offline.writer import journal_runtime_lock

from .queries import build_offline_limbo_payload


class OfflineLimboActionError(RuntimeError):
    pass


def execute_offline_limbo_action(*, action: str) -> dict:
    action_name = str(action or '').strip().lower()
    if action_name not in {'reconcile_sidecar', 'reseal_segment', 'seal_active_segment'}:
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
    runtime_config = OfflineJournalRuntimeConfig(
        root_dir=root_dir,
        stream_name=stream_name,
        segment_max_bytes=getattr(settings, 'OFFLINE_JOURNAL_SEGMENT_MAX_BYTES', 100 * 1024 * 1024),
        limbo_recent_limit=getattr(settings, 'OFFLINE_JOURNAL_LIMBO_RECENT_LIMIT', 50),
    )
    try:
        with journal_runtime_lock(root_dir, stream_name):
            payload = build_offline_limbo_payload()
            limbo = payload.get('limbo') or {}
            rotation = payload.get('rotation') or {}
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
            elif action_name == 'reseal_segment':
                resealed = reseal_segment_from_snapshot(segment_path, snapshot_path)
                performed = bool(resealed)
                if resealed:
                    detail = 'Footer del segmento re-sellado desde el sidecar.'
                else:
                    detail = 'El segmento ya estaba sellado o no tenia footer pendiente.'
            else:
                if not rotation.get('action_allowed'):
                    raise OfflineLimboActionError(
                        str(rotation.get('reason') or 'El runtime no detecta necesidad de rotacion.')
                    )
                runtime = SegmentedJournalRuntime(config=runtime_config)
                sealed_snapshot = runtime.seal_active_segment()
                if not sealed_snapshot:
                    raise OfflineLimboActionError('no hay segmento activo para sellar')
                performed = True
                detail = (
                    'Segmento activo sellado manualmente por condicion de rotacion. '
                    f'{sealed_snapshot.get("segment_id") or segment_id}'
                )
            refreshed = build_offline_limbo_payload()
    except JournalIntegrityError as exc:
        raise OfflineLimboActionError(str(exc)) from exc

    refreshed['action'] = {
        'name': action_name,
        'performed': performed,
        'detail': detail,
    }
    return refreshed
