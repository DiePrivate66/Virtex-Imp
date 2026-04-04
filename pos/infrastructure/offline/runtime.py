from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Any, Mapping

from django.utils import timezone

from pos.infrastructure.offline.journal import (
    JournalIntegrityError,
    SegmentJournal,
    load_snapshot_payload,
    persist_snapshot_payload,
    reconcile_snapshot_with_segment,
    recover_segment_prefix,
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

    def __post_init__(self):
        object.__setattr__(self, 'root_dir', Path(self.root_dir))
        object.__setattr__(self, 'stream_name', _normalize_stream_name(self.stream_name))
        object.__setattr__(self, 'segment_max_bytes', max(1024, int(self.segment_max_bytes)))
        object.__setattr__(self, 'limbo_recent_limit', max(1, int(self.limbo_recent_limit)))


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
    }


def _build_recent_sale_entry(record: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = record.get('payload') or {}
    if str(payload.get('journal_event_type') or '') != 'sale':
        return None
    amount = _parse_decimal(payload.get('sale_total') or payload.get('total'))
    return {
        'event_id': str(record.get('event_id') or ''),
        'created_at': record.get('created_at'),
        'client_transaction_id': str(record.get('client_transaction_id') or ''),
        'queue_session_id': str(record.get('queue_session_id') or ''),
        'session_seq_no': record.get('session_seq_no'),
        'sale_total': f'{amount:.2f}',
        'payment_status': str(payload.get('payment_status') or ''),
        'payment_reference': str(payload.get('payment_reference') or ''),
        'display_name': str(
            payload.get('display_name')
            or payload.get('cliente_nombre')
            or payload.get('sale_label')
            or ''
        )[:120],
    }


def rebuild_limbo_summary_from_records(
    records: tuple[dict[str, Any], ...],
    *,
    limbo_recent_limit: int = DEFAULT_LIMBO_RECENT_LIMIT,
) -> dict[str, Any]:
    total_sales = 0
    amount_total = Decimal('0.00')
    recent_sales: list[dict[str, Any]] = []

    for record in records:
        entry = _build_recent_sale_entry(record)
        if entry is None:
            continue
        total_sales += 1
        amount_total += _parse_decimal(entry['sale_total'])
        recent_sales.append(entry)

    recent_sales = list(reversed(recent_sales[-max(1, limbo_recent_limit):]))
    return {
        'summary_version': LIMBO_SUMMARY_VERSION,
        'total_sales': total_sales,
        'amount_total': f'{amount_total.quantize(Decimal("0.01")):.2f}',
        'recent_sales': recent_sales,
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
        record = journal.append_event(
            event_id=event_id,
            payload=event_payload,
            client_transaction_id=client_transaction_id,
            queue_session_id=queue_session_id,
            session_seq_no=session_seq_no,
            client_created_at_raw=client_created_at_raw,
            client_monotonic_ms=client_monotonic_ms,
            summary=summary,
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
            }

        segment_path, snapshot_path = self._segment_paths(segment_id)
        snapshot = self._reconcile_and_rebuild_summary(segment_id=segment_id)
        return {
            'stream_name': self.config.stream_name,
            'segment_id': segment_id,
            'segment_path': str(segment_path),
            'snapshot_path': str(snapshot_path),
            'record_count': int(snapshot.get('record_count') or 0),
            'sealed': bool(snapshot.get('sealed')),
            'summary': snapshot.get('summary') or _empty_limbo_summary(),
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
            snapshot = _empty_limbo_summary()
        journal = SegmentJournal(
            segment_path=segment_path,
            snapshot_path=snapshot_path,
            segment_id=segment_id,
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
        expected_summary = rebuild_limbo_summary_from_records(
            recovery.records,
            limbo_recent_limit=self.config.limbo_recent_limit,
        )
        if snapshot.get('summary') != expected_summary:
            snapshot['summary'] = expected_summary
            persist_snapshot_payload(snapshot_path, snapshot)
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
