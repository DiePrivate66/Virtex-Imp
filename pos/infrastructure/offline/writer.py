from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Iterator, Mapping

from pos.infrastructure.offline.runtime import OfflineJournalRuntimeConfig, SegmentedJournalRuntime


VALID_OFFLINE_JOURNAL_EVENT_TYPES = {'sale', 'lifecycle'}


@dataclass(frozen=True)
class OfflineJournalEnvelope:
    event_id: str
    journal_event_type: str
    payload: dict[str, Any]
    client_transaction_id: str = ''
    queue_session_id: str = ''
    session_seq_no: int | None = None
    client_created_at_raw: str = ''
    client_monotonic_ms: int | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> 'OfflineJournalEnvelope':
        payload = raw.get('payload') or {}
        if not isinstance(payload, Mapping):
            raise ValueError('payload must be an object')
        journal_event_type = str(
            raw.get('journal_event_type')
            or payload.get('journal_event_type')
            or ''
        ).strip().lower()
        if journal_event_type not in VALID_OFFLINE_JOURNAL_EVENT_TYPES:
            raise ValueError(
                f'journal_event_type must be one of {sorted(VALID_OFFLINE_JOURNAL_EVENT_TYPES)}'
            )
        event_id = str(raw.get('event_id') or '').strip()
        if not event_id:
            raise ValueError('event_id is required')

        session_seq_no = raw.get('session_seq_no')
        if session_seq_no in {'', None}:
            normalized_session_seq_no = None
        else:
            normalized_session_seq_no = int(session_seq_no)

        client_monotonic_ms = raw.get('client_monotonic_ms')
        if client_monotonic_ms in {'', None}:
            normalized_client_monotonic_ms = None
        else:
            normalized_client_monotonic_ms = int(client_monotonic_ms)

        return cls(
            event_id=event_id,
            journal_event_type=journal_event_type,
            payload=dict(payload),
            client_transaction_id=str(raw.get('client_transaction_id') or '').strip(),
            queue_session_id=str(raw.get('queue_session_id') or '').strip(),
            session_seq_no=normalized_session_seq_no,
            client_created_at_raw=str(raw.get('client_created_at_raw') or '').strip(),
            client_monotonic_ms=normalized_client_monotonic_ms,
        )


def append_offline_journal_envelope(
    *,
    config: OfflineJournalRuntimeConfig,
    envelope: OfflineJournalEnvelope,
) -> dict[str, Any]:
    runtime = SegmentedJournalRuntime(config=config)
    with journal_runtime_lock(config.root_dir, config.stream_name):
        if envelope.journal_event_type == 'sale':
            record = runtime.append_sale_event(
                event_id=envelope.event_id,
                payload=envelope.payload,
                client_transaction_id=envelope.client_transaction_id,
                queue_session_id=envelope.queue_session_id,
                session_seq_no=envelope.session_seq_no,
                client_created_at_raw=envelope.client_created_at_raw,
                client_monotonic_ms=envelope.client_monotonic_ms,
            )
        else:
            record = runtime.append_lifecycle_event(
                event_id=envelope.event_id,
                payload=envelope.payload,
                client_transaction_id=envelope.client_transaction_id,
                queue_session_id=envelope.queue_session_id,
                session_seq_no=envelope.session_seq_no,
                client_created_at_raw=envelope.client_created_at_raw,
                client_monotonic_ms=envelope.client_monotonic_ms,
            )
        limbo_view = runtime.get_limbo_view()
    return {
        'event_id': envelope.event_id,
        'journal_event_type': envelope.journal_event_type,
        'record': record,
        'limbo': limbo_view,
    }


@contextmanager
def journal_runtime_lock(root_dir: Path, stream_name: str) -> Iterator[None]:
    lock_path = Path(root_dir) / f'.{stream_name}.capture.lock'
    Path(root_dir).mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        _acquire_file_lock(handle)
        try:
            yield
        finally:
            _release_file_lock(handle)


def _acquire_file_lock(handle) -> None:
    if os.name == 'nt':
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_file_lock(handle) -> None:
    if os.name == 'nt':
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
