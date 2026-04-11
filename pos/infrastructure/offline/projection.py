from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .journal import recover_segment_prefix


DEFAULT_PROJECTION_WINDOW_HOURS = 24


class OfflineProjectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OfflineProjectionConfig:
    root_dir: Path
    stream_name: str = 'sales'
    window_hours: int = DEFAULT_PROJECTION_WINDOW_HOURS

    def __post_init__(self):
        object.__setattr__(self, 'root_dir', Path(self.root_dir))
        object.__setattr__(self, 'stream_name', str(self.stream_name or 'sales').strip() or 'sales')
        object.__setattr__(self, 'window_hours', max(1, int(self.window_hours)))

    @property
    def db_path(self) -> Path:
        return self.root_dir / f'{self.stream_name}.projection.sqlite'


def get_projection_status(*, config: OfflineProjectionConfig) -> dict[str, Any]:
    db_path = config.db_path
    free_bytes = _disk_free_bytes(config.root_dir)
    if not db_path.exists():
        return {
            'available': False,
            'mode': 'journal_only',
            'db_path': str(db_path),
            'window_hours': config.window_hours,
            'reason': 'projection.sqlite ausente',
            'row_count': 0,
            'last_event_id': '',
            'last_record_hash': '',
            'last_segment_id': '',
            'projected_at': '',
            'disk_free_bytes': free_bytes,
        }

    try:
        connection = sqlite3.connect(db_path)
        try:
            row_count = connection.execute('SELECT COUNT(*) FROM sales_projection').fetchone()[0]
            checkpoint = connection.execute(
                '''
                SELECT last_segment_id, last_event_id, last_record_hash, projected_at
                FROM projection_checkpoint
                WHERE stream_name = ?
                ''',
                [config.stream_name],
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        return {
            'available': False,
            'mode': 'journal_only',
            'db_path': str(db_path),
            'window_hours': config.window_hours,
            'reason': f'projection.sqlite invalida: {exc}',
            'row_count': 0,
            'last_event_id': '',
            'last_record_hash': '',
            'last_segment_id': '',
            'projected_at': '',
            'disk_free_bytes': free_bytes,
        }

    checkpoint = checkpoint or ('', '', '', '')
    return {
        'available': True,
        'mode': 'healthy',
        'db_path': str(db_path),
        'window_hours': config.window_hours,
        'reason': '',
        'row_count': int(row_count or 0),
        'last_segment_id': str(checkpoint[0] or ''),
        'last_event_id': str(checkpoint[1] or ''),
        'last_record_hash': str(checkpoint[2] or ''),
        'projected_at': str(checkpoint[3] or ''),
        'disk_free_bytes': free_bytes,
    }


def rebuild_projection_in_place(*, config: OfflineProjectionConfig) -> dict[str, Any]:
    config.root_dir.mkdir(parents=True, exist_ok=True)
    if not _has_projection_rebuild_space(config):
        raise OfflineProjectionError('espacio insuficiente para rebuild in-place de projection.sqlite')

    db_path = config.db_path
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    try:
        _configure_projection_sqlite(connection)
        _initialize_projection_schema(connection)

        included_segment_ids = _resolve_projection_segment_ids(config)
        cutoff = timezone.now() - timedelta(hours=config.window_hours)
        last_segment_id = ''
        last_event_id = ''
        last_record_hash = ''
        row_count = 0

        with connection:
            connection.execute('DELETE FROM sales_projection')
            connection.execute(
                'DELETE FROM projection_checkpoint WHERE stream_name = ?',
                [config.stream_name],
            )
            for segment_id in included_segment_ids:
                segment_path = config.root_dir / f'{segment_id}.jsonl'
                recovery = recover_segment_prefix(segment_path)
                for record in recovery.records:
                    projected_row = _build_projected_sale_row(
                        record=record,
                        segment_id=segment_id,
                        cutoff=cutoff,
                    )
                    if projected_row is None:
                        continue
                    connection.execute(
                        '''
                        INSERT OR REPLACE INTO sales_projection (
                            event_id,
                            segment_id,
                            created_at,
                            client_transaction_id,
                            ticket_number,
                            payment_reference,
                            status,
                            sale_total,
                            display_name,
                            queue_session_id,
                            session_seq_no,
                            window_reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                        projected_row,
                    )
                    row_count += 1
                    last_segment_id = segment_id
                    last_event_id = str(record.get('event_id') or '')
                    last_record_hash = str(record.get('record_hash') or '')

            connection.execute(
                '''
                INSERT INTO projection_checkpoint (
                    stream_name,
                    last_segment_id,
                    last_event_id,
                    last_record_hash,
                    projected_at
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                [
                    config.stream_name,
                    last_segment_id,
                    last_event_id,
                    last_record_hash,
                    timezone.now().isoformat(),
                ],
            )
        connection.execute('PRAGMA incremental_vacuum(64)')
    except sqlite3.DatabaseError as exc:
        raise OfflineProjectionError(str(exc)) from exc
    finally:
        connection.close()

    status = get_projection_status(config=config)
    status['row_count'] = row_count
    return status


def _configure_projection_sqlite(connection: sqlite3.Connection) -> None:
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA auto_vacuum=INCREMENTAL')
    connection.execute('PRAGMA synchronous=NORMAL')
    connection.execute('PRAGMA temp_store=MEMORY')


def _initialize_projection_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        '''
        CREATE TABLE IF NOT EXISTS projection_checkpoint (
            stream_name TEXT PRIMARY KEY,
            last_segment_id TEXT NOT NULL DEFAULT '',
            last_event_id TEXT NOT NULL DEFAULT '',
            last_record_hash TEXT NOT NULL DEFAULT '',
            projected_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS sales_projection (
            event_id TEXT PRIMARY KEY,
            segment_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '',
            client_transaction_id TEXT NOT NULL DEFAULT '',
            ticket_number TEXT NOT NULL DEFAULT '',
            payment_reference TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            sale_total TEXT NOT NULL DEFAULT '0.00',
            display_name TEXT NOT NULL DEFAULT '',
            queue_session_id TEXT NOT NULL DEFAULT '',
            session_seq_no INTEGER NULL,
            window_reason TEXT NOT NULL DEFAULT ''
        );
        '''
    )


def _resolve_projection_segment_ids(config: OfflineProjectionConfig) -> list[str]:
    snapshot_paths = sorted(config.root_dir.glob(f'{config.stream_name}-*.snapshot.json'))
    latest_segment_ids = [
        snapshot_path.name.replace('.snapshot.json', '')
        for snapshot_path in snapshot_paths[-2:]
    ]
    selected = set(latest_segment_ids)
    cutoff = timezone.now() - timedelta(hours=config.window_hours)

    for snapshot_path in snapshot_paths:
        segment_id = snapshot_path.name.replace('.snapshot.json', '')
        segment_path = config.root_dir / f'{segment_id}.jsonl'
        recovery = recover_segment_prefix(segment_path)
        if not recovery.footer:
            selected.add(segment_id)
            continue
        for record in recovery.records:
            payload = record.get('payload') or {}
            if str(payload.get('journal_event_type') or '') != 'sale':
                continue
            created_at = parse_datetime(str(record.get('created_at') or ''))
            if created_at and created_at >= cutoff:
                selected.add(segment_id)
                break

    return sorted(selected)


def _build_projected_sale_row(
    *,
    record: dict[str, Any],
    segment_id: str,
    cutoff: datetime,
) -> tuple[Any, ...] | None:
    payload = record.get('payload') or {}
    if str(payload.get('journal_event_type') or '') != 'sale':
        return None
    created_at = parse_datetime(str(record.get('created_at') or ''))
    if created_at and created_at >= cutoff:
        window_reason = '24h'
    else:
        window_reason = 'segment_window'

    return (
        str(record.get('event_id') or ''),
        segment_id,
        str(record.get('created_at') or ''),
        str(record.get('client_transaction_id') or ''),
        str(payload.get('ticket_number') or payload.get('ticket_no') or payload.get('sale_id') or ''),
        str(payload.get('payment_reference') or ''),
        str(payload.get('payment_status') or payload.get('estado') or ''),
        str(payload.get('sale_total') or payload.get('total') or '0.00'),
        str(payload.get('display_name') or payload.get('cliente_nombre') or payload.get('sale_label') or ''),
        str(record.get('queue_session_id') or ''),
        record.get('session_seq_no'),
        window_reason,
    )


def _has_projection_rebuild_space(config: OfflineProjectionConfig) -> bool:
    journal_size = sum(
        segment_path.stat().st_size
        for segment_path in config.root_dir.glob(f'{config.stream_name}-*.jsonl')
        if segment_path.exists()
    )
    current_db_size = config.db_path.stat().st_size if config.db_path.exists() else 0
    estimated_required = max(2 * 1024 * 1024, int(journal_size * 0.2) + current_db_size)
    return _disk_free_bytes(config.root_dir) >= estimated_required


def _disk_free_bytes(root_dir: Path) -> int:
    return int(shutil.disk_usage(root_dir).free)
