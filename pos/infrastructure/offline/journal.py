from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping
import zlib

from django.utils import timezone


JOURNAL_SCHEMA_VERSION = 1
EVENT_KIND = 'event'
FOOTER_KIND = 'footer'
SIDECAR_FORMAT_VERSION = 'fixed_v1'
DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES = 50
DEFAULT_SIDECAR_MAX_BYTES = 64 * 1024
MAX_RECENT_LOOKUP_TEXT_BYTES = 120
MAX_VERIFICATION_ERROR_BYTES = 255


class JournalIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryResult:
    segment_path: Path
    record_count: int
    last_valid_offset: int
    last_record_hash: str
    rolling_crc32: str
    last_event_id: str
    records: tuple[dict[str, Any], ...]
    footer: dict[str, Any] | None
    truncated_tail: bool
    corrupted_tail: bool
    error_message: str = ''


def _normalize_primitive(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            return value.isoformat(timespec='microseconds')
        return timezone.localtime(value).isoformat(timespec='microseconds')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat(timespec='microseconds')
    if isinstance(value, Path):
        return str(value)
    return value


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(value[key]) for key in sorted(value.keys())}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    return _normalize_primitive(value)


def canonical_json_dumps(value: Any) -> str:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=True,
        separators=(',', ':'),
        sort_keys=False,
    )


def _sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json_dumps(value).encode('utf-8')).hexdigest()


def _crc32_hex(value: Any) -> str:
    checksum = zlib.crc32(canonical_json_dumps(value).encode('utf-8')) & 0xFFFFFFFF
    return f'{checksum:08x}'


def _extend_rolling_crc32(previous_crc32: str, record_hash: str) -> str:
    prior = int(str(previous_crc32 or '00000000'), 16)
    checksum = zlib.crc32(str(record_hash or '').encode('ascii'), prior) & 0xFFFFFFFF
    return f'{checksum:08x}'


def _snapshot_base(
    *,
    segment_path: Path,
    segment_id: str,
    sidecar_max_bytes: int = DEFAULT_SIDECAR_MAX_BYTES,
) -> dict[str, Any]:
    return {
        'schema_version': JOURNAL_SCHEMA_VERSION,
        'sidecar_format_version': SIDECAR_FORMAT_VERSION,
        'sidecar_max_bytes': max(1024, min(int(sidecar_max_bytes), DEFAULT_SIDECAR_MAX_BYTES)),
        'segment_id': segment_id,
        'segment_filename': segment_path.name,
        'record_count': 0,
        'last_offset_confirmed': 0,
        'last_event_id': '',
        'last_record_hash': '',
        'rolling_crc32': '00000000',
        'sealed': False,
        'seal_pending': False,
        'pending_footer': {},
        'summary': {},
        'recent_lookup_ring': _empty_recent_lookup_ring(),
        'recent_lookup_ring_capacity': DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
        'recent_lookup_ring_count': 0,
        'recent_lookup_ring_cursor': -1,
        'ops_metadata': {},
        'receipts': {
            'purge': [],
            'usb_export': [],
        },
        'verify_path_available': True,
        'last_verify_status': 'unknown',
        'last_verify_error': '',
    }


def _truncate_text(value: Any, *, limit: int = MAX_RECENT_LOOKUP_TEXT_BYTES) -> str:
    return str(value or '').strip()[: max(1, int(limit))]


def _empty_recent_lookup_entry() -> dict[str, Any]:
    return {
        'event_id': '',
        'client_transaction_id': '',
        'ticket_number': '',
        'payment_reference': '',
        'segment_id': '',
        'offset': 0,
        'status': '',
        'sale_total': '0.00',
        'created_at': '',
        'display_name_truncated': '',
        'queue_session_id': '',
        'session_seq_no': None,
    }


def _empty_recent_lookup_ring(
    *,
    capacity: int = DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
) -> list[dict[str, Any]]:
    return [_empty_recent_lookup_entry() for _ in range(max(1, int(capacity)))]


def _normalize_recent_lookup_entry(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return _empty_recent_lookup_entry()
    entry = _empty_recent_lookup_entry()
    entry.update(
        {
            'event_id': _truncate_text(raw.get('event_id')),
            'client_transaction_id': _truncate_text(raw.get('client_transaction_id')),
            'ticket_number': _truncate_text(raw.get('ticket_number')),
            'payment_reference': _truncate_text(raw.get('payment_reference')),
            'segment_id': _truncate_text(raw.get('segment_id')),
            'offset': max(0, int(raw.get('offset') or 0)),
            'status': _truncate_text(raw.get('status')),
            'sale_total': _truncate_text(raw.get('sale_total') or '0.00', limit=32),
            'created_at': _truncate_text(raw.get('created_at')),
            'display_name_truncated': _truncate_text(raw.get('display_name_truncated')),
            'queue_session_id': _truncate_text(raw.get('queue_session_id')),
            'session_seq_no': raw.get('session_seq_no'),
        }
    )
    return entry


def _normalize_recent_lookup_ring(
    raw: Any,
    *,
    capacity: int = DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
) -> list[dict[str, Any]]:
    normalized_capacity = max(1, int(capacity))
    source = list(raw or []) if isinstance(raw, list) else []
    entries = [_normalize_recent_lookup_entry(item) for item in source[:normalized_capacity]]
    if len(entries) < normalized_capacity:
        entries.extend(_empty_recent_lookup_ring(capacity=normalized_capacity - len(entries)))
    return entries[:normalized_capacity]


def _normalize_snapshot_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload or {})
    capacity = max(
        1,
        min(
            DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
            int(normalized.get('recent_lookup_ring_capacity') or DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES),
        ),
    )
    ring = _normalize_recent_lookup_ring(
        normalized.get('recent_lookup_ring') or [],
        capacity=capacity,
    )
    raw_cursor = normalized.get('recent_lookup_ring_cursor', -1)
    cursor = -1 if raw_cursor in {'', None} else int(raw_cursor)
    count = max(0, min(int(normalized.get('recent_lookup_ring_count') or 0), capacity))
    if count <= 0:
        cursor = -1
    elif cursor < 0 or cursor >= capacity:
        cursor = count - 1

    normalized['sidecar_format_version'] = SIDECAR_FORMAT_VERSION
    normalized['sidecar_max_bytes'] = max(
        1024,
        min(int(normalized.get('sidecar_max_bytes') or DEFAULT_SIDECAR_MAX_BYTES), DEFAULT_SIDECAR_MAX_BYTES),
    )
    normalized['recent_lookup_ring_capacity'] = capacity
    normalized['recent_lookup_ring'] = ring
    normalized['recent_lookup_ring_count'] = count
    normalized['recent_lookup_ring_cursor'] = cursor
    normalized['ops_metadata'] = dict(normalized.get('ops_metadata') or {})
    receipts = dict(normalized.get('receipts') or {})
    normalized['receipts'] = {
        'purge': list(receipts.get('purge') or [])[-50:],
        'usb_export': list(receipts.get('usb_export') or [])[-50:],
    }
    normalized['verify_path_available'] = bool(normalized.get('verify_path_available', True))
    normalized['last_verify_status'] = _truncate_text(normalized.get('last_verify_status') or 'unknown', limit=32)
    normalized['last_verify_error'] = _truncate_text(
        normalized.get('last_verify_error'),
        limit=MAX_VERIFICATION_ERROR_BYTES,
    )
    return normalized


def _serialize_snapshot_payload(payload: Mapping[str, Any]) -> bytes:
    normalized = _normalize_snapshot_payload(payload)
    encoded = canonical_json_dumps(normalized).encode('utf-8')
    max_bytes = max(1024, int(normalized.get('sidecar_max_bytes') or DEFAULT_SIDECAR_MAX_BYTES))
    if len(encoded) > max_bytes:
        raise JournalIntegrityError(
            f'sidecar exceeded max size: {len(encoded)} > {max_bytes}'
        )
    return encoded


def _append_recent_lookup_entry_to_snapshot(
    snapshot: dict[str, Any],
    *,
    entry: Mapping[str, Any] | None,
) -> None:
    if not entry:
        return
    capacity = max(
        1,
        min(
            DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
            int(snapshot.get('recent_lookup_ring_capacity') or DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES),
        ),
    )
    ring = _normalize_recent_lookup_ring(snapshot.get('recent_lookup_ring') or [], capacity=capacity)
    current_count = max(0, min(int(snapshot.get('recent_lookup_ring_count') or 0), capacity))
    raw_cursor = snapshot.get('recent_lookup_ring_cursor', -1)
    current_cursor = -1 if raw_cursor in {'', None} else int(raw_cursor)
    next_cursor = (current_cursor + 1) % capacity
    ring[next_cursor] = _normalize_recent_lookup_entry(entry)
    snapshot['recent_lookup_ring'] = ring
    snapshot['recent_lookup_ring_capacity'] = capacity
    snapshot['recent_lookup_ring_cursor'] = next_cursor
    snapshot['recent_lookup_ring_count'] = min(capacity, current_count + 1)


def recent_lookup_entries_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    limit: int | None = None,
) -> tuple[dict[str, Any], ...]:
    capacity = max(
        1,
        min(
            DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
            int(snapshot.get('recent_lookup_ring_capacity') or DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES),
        ),
    )
    ring = _normalize_recent_lookup_ring(snapshot.get('recent_lookup_ring') or [], capacity=capacity)
    count = max(0, min(int(snapshot.get('recent_lookup_ring_count') or 0), capacity))
    raw_cursor = snapshot.get('recent_lookup_ring_cursor', -1)
    cursor = -1 if raw_cursor in {'', None} else int(raw_cursor)
    if count <= 0 or cursor < 0:
        return ()

    entries: list[dict[str, Any]] = []
    max_items = min(count, max(1, int(limit))) if limit is not None else count
    for index in range(max_items):
        ring_index = (cursor - index) % capacity
        entry = ring[ring_index]
        if entry.get('event_id'):
            entries.append(entry)
    return tuple(entries)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open('r', encoding='utf-8') as handle:
        return json.load(handle)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _serialize_snapshot_payload(payload)
    with NamedTemporaryFile('w', encoding='utf-8', delete=False, dir=str(path.parent), suffix='.tmp') as handle:
        handle.write(encoded.decode('utf-8'))
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, path)


def load_snapshot_payload(snapshot_path: str | Path) -> dict[str, Any]:
    payload = _read_json_file(Path(snapshot_path)) or {}
    if not payload:
        return {}
    return _normalize_snapshot_payload(payload)


def persist_snapshot_payload(snapshot_path: str | Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_json(Path(snapshot_path), _normalize_snapshot_payload(dict(payload)))


def _read_tail(path: Path, length: int) -> bytes:
    with path.open('rb') as handle:
        handle.seek(max(0, path.stat().st_size - length))
        return handle.read(length)


def _build_event_record(
    *,
    event_id: str,
    payload: Mapping[str, Any],
    prev_record_hash: str = '',
    client_transaction_id: str = '',
    queue_session_id: str = '',
    session_seq_no: int | None = None,
    client_created_at_raw: str = '',
    client_monotonic_ms: int | None = None,
    created_at=None,
) -> dict[str, Any]:
    payload_hash = _sha256_hex(payload)
    body = {
        'schema_version': JOURNAL_SCHEMA_VERSION,
        'kind': EVENT_KIND,
        'event_id': str(event_id or '').strip(),
        'client_transaction_id': str(client_transaction_id or '').strip(),
        'queue_session_id': str(queue_session_id or '').strip(),
        'session_seq_no': session_seq_no,
        'client_created_at_raw': str(client_created_at_raw or '').strip(),
        'client_monotonic_ms': client_monotonic_ms,
        'created_at': created_at or timezone.now(),
        'payload': dict(payload),
        'payload_hash': payload_hash,
        'prev_record_hash': str(prev_record_hash or '').strip(),
    }
    record_hash = _sha256_hex(body)
    envelope = {
        **body,
        'record_hash': record_hash,
    }
    return {
        **envelope,
        'record_crc32': _crc32_hex(envelope),
    }


def _build_footer_record(
    *,
    segment_id: str,
    final_record_hash: str,
    record_count: int,
    segment_crc32: str,
) -> dict[str, Any]:
    body = {
        'schema_version': JOURNAL_SCHEMA_VERSION,
        'kind': FOOTER_KIND,
        'segment_id': segment_id,
        'final_record_hash': final_record_hash,
        'record_count': int(record_count),
        'segment_crc32': str(segment_crc32 or '00000000'),
    }
    footer_hash = _sha256_hex(body)
    envelope = {
        **body,
        'footer_hash': footer_hash,
    }
    return {
        **envelope,
        'record_crc32': _crc32_hex(envelope),
    }


def _encode_record_line(record: Mapping[str, Any]) -> bytes:
    return (canonical_json_dumps(record) + '\n').encode('utf-8')


def _validate_event_record(record: Mapping[str, Any], *, expected_prev_record_hash: str) -> str:
    payload = record.get('payload') or {}
    if record.get('payload_hash') != _sha256_hex(payload):
        raise JournalIntegrityError('payload_hash mismatch')
    body = {
        'schema_version': record.get('schema_version'),
        'kind': record.get('kind'),
        'event_id': record.get('event_id'),
        'client_transaction_id': record.get('client_transaction_id', ''),
        'queue_session_id': record.get('queue_session_id', ''),
        'session_seq_no': record.get('session_seq_no'),
        'client_created_at_raw': record.get('client_created_at_raw', ''),
        'client_monotonic_ms': record.get('client_monotonic_ms'),
        'created_at': record.get('created_at'),
        'payload': payload,
        'payload_hash': record.get('payload_hash'),
        'prev_record_hash': record.get('prev_record_hash', ''),
    }
    computed_record_hash = _sha256_hex(body)
    if record.get('record_hash') != computed_record_hash:
        raise JournalIntegrityError('record_hash mismatch')
    envelope = {
        **body,
        'record_hash': computed_record_hash,
    }
    if record.get('record_crc32') != _crc32_hex(envelope):
        raise JournalIntegrityError('record_crc32 mismatch')
    if (record.get('prev_record_hash') or '') != expected_prev_record_hash:
        raise JournalIntegrityError('prev_record_hash mismatch')
    return computed_record_hash


def _validate_footer_record(
    record: Mapping[str, Any],
    *,
    final_record_hash: str,
    record_count: int,
    segment_crc32: str,
) -> None:
    body = {
        'schema_version': record.get('schema_version'),
        'kind': record.get('kind'),
        'segment_id': record.get('segment_id'),
        'final_record_hash': record.get('final_record_hash'),
        'record_count': record.get('record_count'),
        'segment_crc32': record.get('segment_crc32', '00000000'),
    }
    footer_hash = _sha256_hex(body)
    if record.get('footer_hash') != footer_hash:
        raise JournalIntegrityError('footer_hash mismatch')
    envelope = {
        **body,
        'footer_hash': footer_hash,
    }
    if record.get('record_crc32') != _crc32_hex(envelope):
        raise JournalIntegrityError('footer_crc32 mismatch')
    if record.get('final_record_hash') != final_record_hash:
        raise JournalIntegrityError('footer final_record_hash mismatch')
    if int(record.get('record_count') or 0) != int(record_count):
        raise JournalIntegrityError('footer record_count mismatch')
    if str(record.get('segment_crc32') or '00000000') != str(segment_crc32 or '00000000'):
        raise JournalIntegrityError('footer segment_crc32 mismatch')


def recover_segment_prefix(segment_path: str | Path) -> RecoveryResult:
    path = Path(segment_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return RecoveryResult(
            segment_path=path,
            record_count=0,
            last_valid_offset=0,
            last_record_hash='',
            rolling_crc32='00000000',
            last_event_id='',
            records=(),
            footer=None,
            truncated_tail=False,
            corrupted_tail=False,
            error_message='',
        )

    valid_records: list[dict[str, Any]] = []
    last_valid_offset = 0
    last_record_hash = ''
    rolling_crc32 = '00000000'
    last_event_id = ''
    footer = None
    truncated_tail = False
    corrupted_tail = False
    error_message = ''
    current_offset = 0

    with path.open('rb') as handle:
        for raw_line in handle:
            line_start_offset = current_offset
            current_offset += len(raw_line)
            if not raw_line.endswith(b'\n'):
                truncated_tail = True
                error_message = 'truncated tail'
                break
            try:
                record = json.loads(raw_line.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                corrupted_tail = True
                error_message = f'invalid json: {exc}'
                break

            kind = record.get('kind')
            try:
                if kind == EVENT_KIND:
                    last_record_hash = _validate_event_record(
                        record,
                        expected_prev_record_hash=last_record_hash,
                    )
                    rolling_crc32 = _extend_rolling_crc32(rolling_crc32, last_record_hash)
                    event_record = dict(record)
                    event_record['record_offset'] = line_start_offset
                    valid_records.append(event_record)
                    last_event_id = str(record.get('event_id') or '')
                elif kind == FOOTER_KIND:
                    _validate_footer_record(
                        record,
                        final_record_hash=last_record_hash,
                        record_count=len(valid_records),
                        segment_crc32=rolling_crc32,
                    )
                    footer = record
                else:
                    raise JournalIntegrityError('unknown kind')
            except JournalIntegrityError as exc:
                corrupted_tail = True
                error_message = str(exc)
                break

            last_valid_offset = current_offset
            if footer is not None:
                trailing = handle.read()
                if trailing:
                    corrupted_tail = True
                    error_message = 'trailing bytes after footer'
                break

    return RecoveryResult(
        segment_path=path,
        record_count=len(valid_records),
        last_valid_offset=last_valid_offset,
        last_record_hash=last_record_hash,
        rolling_crc32=rolling_crc32,
        last_event_id=last_event_id,
        records=tuple(valid_records),
        footer=footer,
        truncated_tail=truncated_tail,
        corrupted_tail=corrupted_tail,
        error_message=error_message,
    )


def reconcile_snapshot_with_segment(
    segment_path: str | Path,
    snapshot_path: str | Path,
    *,
    segment_id: str | None = None,
    sidecar_max_bytes: int = DEFAULT_SIDECAR_MAX_BYTES,
) -> dict[str, Any]:
    segment_file = Path(segment_path)
    snapshot_file = Path(snapshot_path)
    recovery = recover_segment_prefix(segment_file)
    snapshot = _read_json_file(snapshot_file) or {}
    effective_segment_id = segment_id or snapshot.get('segment_id') or segment_file.stem

    if int(snapshot.get('last_offset_confirmed') or 0) > recovery.last_valid_offset:
        raise JournalIntegrityError('snapshot claims confirmed data beyond valid journal prefix')

    reconciled = _snapshot_base(
        segment_path=segment_file,
        segment_id=effective_segment_id,
        sidecar_max_bytes=sidecar_max_bytes,
    )
    reconciled.update(
        {
            'record_count': recovery.record_count,
            'last_offset_confirmed': recovery.last_valid_offset,
            'last_event_id': recovery.last_event_id,
            'last_record_hash': recovery.last_record_hash,
            'rolling_crc32': recovery.rolling_crc32,
            'sealed': bool(recovery.footer),
            'seal_pending': False,
            'pending_footer': {},
            'summary': snapshot.get('summary', {}),
            'recent_lookup_ring': snapshot.get('recent_lookup_ring') or [],
            'recent_lookup_ring_capacity': snapshot.get(
                'recent_lookup_ring_capacity',
                DEFAULT_RECENT_LOOKUP_RING_MAX_ENTRIES,
            ),
            'recent_lookup_ring_count': snapshot.get('recent_lookup_ring_count', 0),
            'recent_lookup_ring_cursor': snapshot.get('recent_lookup_ring_cursor', -1),
            'ops_metadata': snapshot.get('ops_metadata') or {},
            'receipts': snapshot.get('receipts') or {},
            'verify_path_available': snapshot.get('verify_path_available', True),
            'last_verify_status': snapshot.get('last_verify_status', 'unknown'),
            'last_verify_error': snapshot.get('last_verify_error', ''),
        }
    )

    pending_footer = snapshot.get('pending_footer') or {}
    if (
        not recovery.footer
        and snapshot.get('seal_pending')
        and pending_footer.get('final_record_hash') == recovery.last_record_hash
        and int(pending_footer.get('record_count') or 0) == recovery.record_count
        and str(pending_footer.get('segment_crc32') or '00000000') == recovery.rolling_crc32
    ):
        reconciled['seal_pending'] = True
        reconciled['pending_footer'] = pending_footer

    _atomic_write_json(snapshot_file, reconciled)
    return reconciled


def reseal_segment_from_snapshot(segment_path: str | Path, snapshot_path: str | Path) -> bool:
    segment_file = Path(segment_path)
    snapshot_file = Path(snapshot_path)
    recovery = recover_segment_prefix(segment_file)
    snapshot = reconcile_snapshot_with_segment(segment_file, snapshot_file)
    if recovery.footer or snapshot.get('sealed'):
        return False
    if not snapshot.get('seal_pending'):
        return False

    pending_footer = snapshot.get('pending_footer') or {}
    if pending_footer.get('final_record_hash') != recovery.last_record_hash:
        raise JournalIntegrityError('pending footer hash does not match active segment tail')
    if int(pending_footer.get('record_count') or 0) != recovery.record_count:
        raise JournalIntegrityError('pending footer count does not match active segment tail')
    if str(pending_footer.get('segment_crc32') or '00000000') != recovery.rolling_crc32:
        raise JournalIntegrityError('pending footer checksum does not match active segment tail')

    footer_record = _build_footer_record(
        segment_id=snapshot.get('segment_id') or segment_file.stem,
        final_record_hash=recovery.last_record_hash,
        record_count=recovery.record_count,
        segment_crc32=recovery.rolling_crc32,
    )
    line = _encode_record_line(footer_record)
    with segment_file.open('ab') as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    if _read_tail(segment_file, len(line)) != line:
        raise JournalIntegrityError('tail verification failed while resealing segment')

    snapshot['sealed'] = True
    snapshot['seal_pending'] = False
    snapshot['pending_footer'] = {}
    snapshot['last_offset_confirmed'] = segment_file.stat().st_size
    _atomic_write_json(snapshot_file, snapshot)
    return True


class SegmentJournal:
    def __init__(
        self,
        *,
        segment_path: str | Path,
        snapshot_path: str | Path,
        segment_id: str | None = None,
        sidecar_max_bytes: int = DEFAULT_SIDECAR_MAX_BYTES,
    ):
        self.segment_path = Path(segment_path)
        self.snapshot_path = Path(snapshot_path)
        self.segment_id = segment_id or self.segment_path.stem
        self.sidecar_max_bytes = max(1024, min(int(sidecar_max_bytes), DEFAULT_SIDECAR_MAX_BYTES))
        self.segment_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        reconcile_snapshot_with_segment(
            self.segment_path,
            self.snapshot_path,
            segment_id=self.segment_id,
            sidecar_max_bytes=self.sidecar_max_bytes,
        )

    def append_event(
        self,
        *,
        event_id: str,
        payload: Mapping[str, Any],
        client_transaction_id: str = '',
        queue_session_id: str = '',
        session_seq_no: int | None = None,
        client_created_at_raw: str = '',
        client_monotonic_ms: int | None = None,
        summary: Mapping[str, Any] | None = None,
        recent_lookup_entry: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = reconcile_snapshot_with_segment(
            self.segment_path,
            self.snapshot_path,
            segment_id=self.segment_id,
            sidecar_max_bytes=self.sidecar_max_bytes,
        )
        if snapshot.get('sealed'):
            raise JournalIntegrityError('cannot append to sealed segment')

        record = _build_event_record(
            event_id=event_id,
            payload=payload,
            prev_record_hash=snapshot.get('last_record_hash', ''),
            client_transaction_id=client_transaction_id,
            queue_session_id=queue_session_id,
            session_seq_no=session_seq_no,
            client_created_at_raw=client_created_at_raw,
            client_monotonic_ms=client_monotonic_ms,
        )
        line = _encode_record_line(record)
        record_offset = int(snapshot.get('last_offset_confirmed') or 0)
        with self.segment_path.open('ab') as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if _read_tail(self.segment_path, len(line)) != line:
            snapshot['verify_path_available'] = True
            snapshot['last_verify_status'] = 'failed'
            snapshot['last_verify_error'] = 'tail verification failed after append'
            _atomic_write_json(self.snapshot_path, snapshot)
            raise JournalIntegrityError('tail verification failed after append')

        snapshot.update(
            {
                'record_count': int(snapshot.get('record_count') or 0) + 1,
                'last_offset_confirmed': self.segment_path.stat().st_size,
                'last_event_id': record['event_id'],
                'last_record_hash': record['record_hash'],
                'rolling_crc32': _extend_rolling_crc32(
                    snapshot.get('rolling_crc32', '00000000'),
                    record['record_hash'],
                ),
                'summary': dict(summary or snapshot.get('summary') or {}),
                'verify_path_available': True,
                'last_verify_status': 'verified',
                'last_verify_error': '',
            }
        )
        _append_recent_lookup_entry_to_snapshot(
            snapshot,
            entry={
                **dict(recent_lookup_entry or {}),
                'segment_id': self.segment_id,
                'offset': record_offset,
            } if recent_lookup_entry else None,
        )
        _atomic_write_json(self.snapshot_path, snapshot)
        return record

    def prepare_seal(self, *, summary: Mapping[str, Any] | None = None) -> dict[str, Any]:
        snapshot = reconcile_snapshot_with_segment(
            self.segment_path,
            self.snapshot_path,
            segment_id=self.segment_id,
            sidecar_max_bytes=self.sidecar_max_bytes,
        )
        if snapshot.get('sealed'):
            return snapshot
        snapshot['seal_pending'] = True
        snapshot['pending_footer'] = {
            'segment_id': self.segment_id,
            'final_record_hash': snapshot.get('last_record_hash', ''),
            'record_count': int(snapshot.get('record_count') or 0),
            'segment_crc32': snapshot.get('rolling_crc32', '00000000'),
        }
        if summary is not None:
            snapshot['summary'] = dict(summary)
        _atomic_write_json(self.snapshot_path, snapshot)
        return snapshot

    def seal(self) -> dict[str, Any]:
        self.prepare_seal()
        reseal_segment_from_snapshot(self.segment_path, self.snapshot_path)
        return _read_json_file(self.snapshot_path) or _snapshot_base(
            segment_path=self.segment_path,
            segment_id=self.segment_id,
            sidecar_max_bytes=self.sidecar_max_bytes,
        )

    def recover(self) -> RecoveryResult:
        return recover_segment_prefix(self.segment_path)
