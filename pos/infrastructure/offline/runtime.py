from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Any, Mapping

from django.utils import timezone

from pos.infrastructure.offline.journal import (
    DEFAULT_SIDECAR_MAX_BYTES,
    DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
    JournalIntegrityError,
    SegmentJournal,
    load_snapshot_payload,
    persist_snapshot_payload,
    recent_lookup_entries_from_snapshot,
    reconcile_snapshot_with_segment,
    recover_segment_prefix,
)
from pos.infrastructure.offline.projection import (
    DEFAULT_PROJECTION_WINDOW_HOURS,
    OfflineProjectionConfig,
    get_projection_status,
)


DEFAULT_SEGMENT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_LIMBO_RECENT_LIMIT = 50
LIMBO_SUMMARY_VERSION = 'limbo_sales_v1'


@dataclass(frozen=True)
class OfflineJournalRuntimeConfig:
    root_dir: Path
    stream_name: str = 'sales'
    segment_max_bytes: int = DEFAULT_SEGMENT_MAX_BYTES
    limbo_recent_limit: int = DEFAULT_LIMBO_RECENT_LIMIT
    sidecar_max_bytes: int = DEFAULT_SIDECAR_MAX_BYTES
    projection_window_hours: int = DEFAULT_PROJECTION_WINDOW_HOURS

    def __post_init__(self):
        object.__setattr__(self, 'root_dir', Path(self.root_dir))
        object.__setattr__(self, 'stream_name', _normalize_stream_name(self.stream_name))
        object.__setattr__(self, 'segment_max_bytes', max(1024, int(self.segment_max_bytes)))
        object.__setattr__(self, 'limbo_recent_limit', max(1, int(self.limbo_recent_limit)))
        object.__setattr__(self, 'sidecar_max_bytes', max(1024, min(int(self.sidecar_max_bytes), DEFAULT_SIDECAR_MAX_BYTES)))
        object.__setattr__(self, 'projection_window_hours', max(1, int(self.projection_window_hours)))


def _normalize_stream_name(value: str) -> str:
    normalized = re.sub(r'[^A-Za-z0-9_-]', '-', str(value or '').strip())
    normalized = re.sub(r'-{2,}', '-', normalized).strip('-')
    return normalized or 'sales'


def _parse_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(Decimal('0.01'))
    try:
        return Decimal(str(value or '0')).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return Decimal('0.00')


def _empty_limbo_summary() -> dict[str, Any]:
    return {
        'summary_version': LIMBO_SUMMARY_VERSION,
        'total_sales': 0,
        'amount_total': '0.00',
        'recent_sales': [],
        'recent_lookup_count': 0,
        'recent_lookup_capacity': DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
    }


def _build_recent_lookup_entry_from_record(
    record: Mapping[str, Any],
    *,
    segment_id: str = '',
) -> dict[str, Any] | None:
    payload = record.get('payload') or {}
    if str(payload.get('journal_event_type') or '') != 'sale':
        return None
    amount = _parse_decimal(payload.get('sale_total') or payload.get('total'))
    return {
        'event_id': str(record.get('event_id') or ''),
        'client_transaction_id': str(record.get('client_transaction_id') or ''),
        'ticket_number': str(
            payload.get('ticket_number')
            or payload.get('ticket_no')
            or payload.get('sale_id')
            or ''
        )[:40],
        'payment_reference': str(payload.get('payment_reference') or '')[:120],
        'segment_id': str(segment_id or payload.get('segment_id') or '')[:120],
        'offset': int(record.get('record_offset') or 0),
        'status': str(payload.get('payment_status') or payload.get('estado') or '')[:60],
        'sale_total': f'{amount:.2f}',
        'created_at': record.get('created_at'),
        'display_name_truncated': str(
            payload.get('display_name')
            or payload.get('cliente_nombre')
            or payload.get('sale_label')
            or ''
        )[:120],
        'queue_session_id': str(record.get('queue_session_id') or ''),
        'session_seq_no': record.get('session_seq_no'),
    }


def _build_recent_sale_entry_from_lookup(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'event_id': str(entry.get('event_id') or ''),
        'created_at': entry.get('created_at'),
        'client_transaction_id': str(entry.get('client_transaction_id') or ''),
        'queue_session_id': str(entry.get('queue_session_id') or ''),
        'session_seq_no': entry.get('session_seq_no'),
        'sale_total': f'{_parse_decimal(entry.get("sale_total")):.2f}',
        'payment_status': str(entry.get('status') or ''),
        'payment_reference': str(entry.get('payment_reference') or ''),
        'display_name': str(entry.get('display_name_truncated') or '')[:120],
        'ticket_number': str(entry.get('ticket_number') or ''),
        'segment_id': str(entry.get('segment_id') or ''),
        'offset': int(entry.get('offset') or 0),
    }


def _decorate_summary_with_recent_lookup(
    summary: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    limit: int,
) -> dict[str, Any]:
    decorated = _empty_limbo_summary()
    decorated.update(summary or {})
    decorated['summary_version'] = LIMBO_SUMMARY_VERSION
    recent_lookup = recent_lookup_entries_from_snapshot(snapshot, limit=limit)
    decorated['recent_sales'] = [
        _build_recent_sale_entry_from_lookup(entry)
        for entry in recent_lookup
    ]
    decorated['recent_lookup_count'] = min(
        int(snapshot.get('recent_lookup_ring_count') or 0),
        int(snapshot.get('recent_lookup_ring_capacity') or DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES),
    )
    decorated['recent_lookup_capacity'] = int(
        snapshot.get('recent_lookup_ring_capacity') or DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES
    )
    return decorated


def rebuild_limbo_summary_from_records(
    records: tuple[dict[str, Any], ...],
    *,
    limbo_recent_limit: int = DEFAULT_LIMBO_RECENT_LIMIT,
    segment_id: str = '',
) -> dict[str, Any]:
    total_sales = 0
    amount_total = Decimal('0.00')
    recent_entries: list[dict[str, Any]] = []

    for record in records:
        entry = _build_recent_lookup_entry_from_record(record, segment_id=segment_id)
        if entry is None:
            continue
        total_sales += 1
        amount_total += _parse_decimal(entry['sale_total'])
        recent_entries.append(entry)

    recent_sales = [
        _build_recent_sale_entry_from_lookup(entry)
        for entry in reversed(recent_entries[-max(1, limbo_recent_limit):])
    ]
    return {
        'summary_version': LIMBO_SUMMARY_VERSION,
        'total_sales': total_sales,
        'amount_total': f'{amount_total.quantize(Decimal("0.01")):.2f}',
        'recent_sales': recent_sales,
        'recent_lookup_count': min(len(recent_entries), DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES),
        'recent_lookup_capacity': DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
    }


def rebuild_limbo_state_from_records(
    records: tuple[dict[str, Any], ...],
    *,
    limbo_recent_limit: int = DEFAULT_LIMBO_RECENT_LIMIT,
    segment_id: str = '',
) -> dict[str, Any]:
    summary = rebuild_limbo_summary_from_records(
        records,
        limbo_recent_limit=limbo_recent_limit,
        segment_id=segment_id,
    )
    recent_entries: list[dict[str, Any]] = []
    for record in records:
        entry = _build_recent_lookup_entry_from_record(record, segment_id=segment_id)
        if entry is not None:
            recent_entries.append(entry)

    bounded_entries = recent_entries[-DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES:]
    cursor = len(bounded_entries) - 1
    padded_ring = list(bounded_entries) + [
        {}
        for _ in range(DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES - len(bounded_entries))
    ]
    return {
        'summary': summary,
        'recent_lookup_ring': padded_ring,
        'recent_lookup_ring_capacity': DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
        'recent_lookup_ring_count': len(bounded_entries),
        'recent_lookup_ring_cursor': cursor,
    }


class SegmentedJournalRuntime:
    def __init__(self, *, config: OfflineJournalRuntimeConfig):
        self.config = config
        self.config.root_dir.mkdir(parents=True, exist_ok=True)
        self._active_segment_id: str | None = None

    def append_sale_event(
        self,
        *,
        event_id: str,
        payload: Mapping[str, Any],
        client_transaction_id: str = '',
        queue_session_id: str = '',
        session_seq_no: int | None = None,
        client_created_at_raw: str = '',
        client_monotonic_ms: int | None = None,
    ) -> dict[str, Any]:
        journal, snapshot = self._ensure_active_journal()
        event_payload = dict(payload)
        event_payload['journal_event_type'] = 'sale'
        event_payload['sale_total'] = f'{_parse_decimal(payload.get("sale_total") or payload.get("total")):.2f}'
        summary = self._build_next_sale_summary(
            snapshot.get('summary') or {},
            event_id=event_id,
            payload=event_payload,
            client_transaction_id=client_transaction_id,
            queue_session_id=queue_session_id,
            session_seq_no=session_seq_no,
        )
        recent_lookup_entry = self._build_next_recent_lookup_entry(
            event_id=event_id,
            payload=event_payload,
            client_transaction_id=client_transaction_id,
            queue_session_id=queue_session_id,
            session_seq_no=session_seq_no,
        )
        record = journal.append_event(
            event_id=event_id,
            payload=event_payload,
            client_transaction_id=client_transaction_id,
            queue_session_id=queue_session_id,
            session_seq_no=session_seq_no,
            client_created_at_raw=client_created_at_raw,
            client_monotonic_ms=client_monotonic_ms,
            summary=summary,
            recent_lookup_entry=recent_lookup_entry,
        )
        self._seal_active_if_oversized(journal=journal)
        return record

    def append_lifecycle_event(
        self,
        *,
        event_id: str,
        payload: Mapping[str, Any],
        client_transaction_id: str = '',
        queue_session_id: str = '',
        session_seq_no: int | None = None,
        client_created_at_raw: str = '',
        client_monotonic_ms: int | None = None,
    ) -> dict[str, Any]:
        journal, snapshot = self._ensure_active_journal()
        event_payload = dict(payload)
        event_payload['journal_event_type'] = str(payload.get('journal_event_type') or 'lifecycle')
        record = journal.append_event(
            event_id=event_id,
            payload=event_payload,
            client_transaction_id=client_transaction_id,
            queue_session_id=queue_session_id,
            session_seq_no=session_seq_no,
            client_created_at_raw=client_created_at_raw,
            client_monotonic_ms=client_monotonic_ms,
            summary=snapshot.get('summary') or _empty_limbo_summary(),
        )
        self._seal_active_if_oversized(journal=journal)
        return record

    def get_limbo_view(self) -> dict[str, Any]:
        segment_id = self._discover_active_segment_id()
        if not segment_id:
            return {
                'stream_name': self.config.stream_name,
                'segment_id': '',
                'segment_path': '',
                'snapshot_path': '',
                'record_count': 0,
                'sealed': False,
                'summary': _empty_limbo_summary(),
                'recent_lookup_ring': [],
                'verify_path_available': False,
                'last_verify_status': 'unknown',
                'last_verify_error': '',
                'mode': 'journal_only',
                'projection': self._build_projection_status(),
            }

        segment_path, snapshot_path = self._segment_paths(segment_id)
        snapshot = self._reconcile_and_rebuild_summary(segment_id=segment_id)
        projection_status = self._build_projection_status()
        return {
            'stream_name': self.config.stream_name,
            'segment_id': segment_id,
            'segment_path': str(segment_path),
            'snapshot_path': str(snapshot_path),
            'record_count': int(snapshot.get('record_count') or 0),
            'sealed': bool(snapshot.get('sealed')),
            'summary': _decorate_summary_with_recent_lookup(
                snapshot.get('summary') or {},
                snapshot=snapshot,
                limit=self.config.limbo_recent_limit,
            ),
            'recent_lookup_ring': list(recent_lookup_entries_from_snapshot(snapshot)),
            'verify_path_available': bool(snapshot.get('verify_path_available', True)),
            'last_verify_status': str(snapshot.get('last_verify_status') or 'unknown'),
            'last_verify_error': str(snapshot.get('last_verify_error') or ''),
            'mode': 'healthy' if projection_status.get('available') else 'journal_only',
            'projection': projection_status,
        }

    def seal_active_segment(self) -> dict[str, Any] | None:
        segment_id = self._discover_active_segment_id()
        if not segment_id:
            return None
        segment_path, snapshot_path = self._segment_paths(segment_id)
        snapshot = self._reconcile_and_rebuild_summary(segment_id=segment_id)
        if snapshot.get('sealed'):
            return snapshot
        journal = SegmentJournal(
            segment_path=segment_path,
            snapshot_path=snapshot_path,
            segment_id=segment_id,
            sidecar_max_bytes=self.config.sidecar_max_bytes,
        )
        sealed_snapshot = journal.seal()
        self._active_segment_id = None
        return sealed_snapshot

    def _ensure_active_journal(self) -> tuple[SegmentJournal, dict[str, Any]]:
        segment_id = self._discover_active_segment_id()
        if not segment_id:
            segment_id = self._next_segment_id()
        segment_path, snapshot_path = self._segment_paths(segment_id)
        snapshot = self._reconcile_and_rebuild_summary(segment_id=segment_id)
        if snapshot.get('sealed'):
            segment_id = self._next_segment_id()
            segment_path, snapshot_path = self._segment_paths(segment_id)
            snapshot = load_snapshot_payload(snapshot_path) or {}
        journal = SegmentJournal(
            segment_path=segment_path,
            snapshot_path=snapshot_path,
            segment_id=segment_id,
            sidecar_max_bytes=self.config.sidecar_max_bytes,
        )
        self._active_segment_id = segment_id
        snapshot_payload = load_snapshot_payload(snapshot_path)
        return journal, snapshot_payload

    def _seal_active_if_oversized(self, *, journal: SegmentJournal) -> None:
        if not journal.segment_path.exists():
            return
        if journal.segment_path.stat().st_size < self.config.segment_max_bytes:
            return
        journal.seal()
        self._active_segment_id = None

    def _reconcile_and_rebuild_summary(self, *, segment_id: str) -> dict[str, Any]:
        segment_path, snapshot_path = self._segment_paths(segment_id)
        snapshot = reconcile_snapshot_with_segment(segment_path, snapshot_path, segment_id=segment_id)
        recovery = recover_segment_prefix(segment_path)
        expected_state = rebuild_limbo_state_from_records(
            recovery.records,
            limbo_recent_limit=self.config.limbo_recent_limit,
            segment_id=segment_id,
        )
        if (
            snapshot.get('summary') != expected_state['summary']
            or snapshot.get('recent_lookup_ring') != expected_state['recent_lookup_ring']
            or int(snapshot.get('recent_lookup_ring_count') or 0) != expected_state['recent_lookup_ring_count']
            or (
                -1 if snapshot.get('recent_lookup_ring_cursor') in {'', None}
                else int(snapshot.get('recent_lookup_ring_cursor'))
            ) != expected_state['recent_lookup_ring_cursor']
        ):
            snapshot['summary'] = expected_state['summary']
            snapshot['recent_lookup_ring'] = expected_state['recent_lookup_ring']
            snapshot['recent_lookup_ring_capacity'] = expected_state['recent_lookup_ring_capacity']
            snapshot['recent_lookup_ring_count'] = expected_state['recent_lookup_ring_count']
            snapshot['recent_lookup_ring_cursor'] = expected_state['recent_lookup_ring_cursor']
            persist_snapshot_payload(snapshot_path, snapshot)
            snapshot = load_snapshot_payload(snapshot_path)
        return snapshot

    def _discover_active_segment_id(self) -> str:
        if self._active_segment_id:
            return self._active_segment_id
        snapshot_paths = sorted(self.config.root_dir.glob(f'{self.config.stream_name}-*.snapshot.json'))
        latest_segment_id = ''
        for snapshot_path in reversed(snapshot_paths):
            snapshot = load_snapshot_payload(snapshot_path)
            segment_id = str(snapshot.get('segment_id') or snapshot_path.name.replace('.snapshot.json', ''))
            if not latest_segment_id:
                latest_segment_id = segment_id
            if not snapshot.get('sealed'):
                self._active_segment_id = segment_id
                return segment_id
        if latest_segment_id:
            self._active_segment_id = latest_segment_id
            return latest_segment_id
        return ''

    def _next_segment_id(self) -> str:
        today = timezone.localdate().strftime('%Y%m%d')
        prefix = f'{self.config.stream_name}-{today}-'
        sequence_numbers: list[int] = []
        for segment_path in self.config.root_dir.glob(f'{self.config.stream_name}-*.jsonl'):
            name = segment_path.stem
            if not name.startswith(prefix):
                continue
            try:
                sequence_numbers.append(int(name.rsplit('-', 1)[-1]))
            except ValueError:
                continue
        next_sequence = (max(sequence_numbers) + 1) if sequence_numbers else 1
        return f'{self.config.stream_name}-{today}-{next_sequence:03d}'

    def _segment_paths(self, segment_id: str) -> tuple[Path, Path]:
        return (
            self.config.root_dir / f'{segment_id}.jsonl',
            self.config.root_dir / f'{segment_id}.snapshot.json',
        )

    def _build_next_sale_summary(
        self,
        summary: Mapping[str, Any],
        *,
        event_id: str,
        payload: Mapping[str, Any],
        client_transaction_id: str,
        queue_session_id: str,
        session_seq_no: int | None,
    ) -> dict[str, Any]:
        normalized = _empty_limbo_summary()
        normalized.update(summary or {})
        normalized['summary_version'] = LIMBO_SUMMARY_VERSION
        normalized['total_sales'] = int(normalized.get('total_sales') or 0) + 1
        running_total = _parse_decimal(normalized.get('amount_total')) + _parse_decimal(payload.get('sale_total'))
        normalized['amount_total'] = f'{running_total:.2f}'
        recent_sales = list(normalized.get('recent_sales') or [])
        recent_sales.insert(
            0,
            {
                'event_id': event_id,
                'created_at': payload.get('created_at'),
                'client_transaction_id': client_transaction_id,
                'queue_session_id': queue_session_id,
                'session_seq_no': session_seq_no,
                'sale_total': f'{_parse_decimal(payload.get("sale_total")):.2f}',
                'payment_status': str(payload.get('payment_status') or ''),
                'payment_reference': str(payload.get('payment_reference') or ''),
                'display_name': str(
                    payload.get('display_name')
                    or payload.get('cliente_nombre')
                    or payload.get('sale_label')
                    or ''
                )[:120],
            },
        )
        normalized['recent_sales'] = recent_sales[: self.config.limbo_recent_limit]
        return normalized

    def _build_next_recent_lookup_entry(
        self,
        *,
        event_id: str,
        payload: Mapping[str, Any],
        client_transaction_id: str,
        queue_session_id: str,
        session_seq_no: int | None,
    ) -> dict[str, Any]:
        return {
            'event_id': event_id,
            'client_transaction_id': client_transaction_id,
            'ticket_number': str(
                payload.get('ticket_number')
                or payload.get('ticket_no')
                or payload.get('sale_id')
                or ''
            )[:40],
            'payment_reference': str(payload.get('payment_reference') or '')[:120],
            'status': str(payload.get('payment_status') or payload.get('estado') or '')[:60],
            'sale_total': f'{_parse_decimal(payload.get("sale_total")):.2f}',
            'created_at': payload.get('created_at'),
            'display_name_truncated': str(
                payload.get('display_name')
                or payload.get('cliente_nombre')
                or payload.get('sale_label')
                or ''
            )[:120],
            'queue_session_id': queue_session_id,
            'session_seq_no': session_seq_no,
        }

    def _build_projection_status(self) -> dict[str, Any]:
        return get_projection_status(
            config=OfflineProjectionConfig(
                root_dir=self.config.root_dir,
                stream_name=self.config.stream_name,
                window_hours=self.config.projection_window_hours,
            )
        )
