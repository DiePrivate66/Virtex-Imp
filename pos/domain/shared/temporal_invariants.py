from __future__ import annotations

import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.dateparse import parse_datetime


def normalize_queue_session_id(value: str | None) -> str:
    session_id = re.sub(r'[^A-Za-z0-9_-]', '', str(value or '').strip())
    return session_id[:64]


def normalize_session_seq_no(value) -> int | None:
    if value in (None, ''):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def normalize_client_monotonic_ms(value) -> int | None:
    if value in (None, ''):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def parse_client_created_at_raw(value: str | None, *, timezone_name: str | None = None):
    raw_value = str(value or '').strip()
    if not raw_value:
        return '', None

    parsed = parse_datetime(raw_value)
    if parsed is None:
        return raw_value[:64], None
    if timezone.is_naive(parsed):
        tzinfo = ZoneInfo(timezone_name) if timezone_name else timezone.get_current_timezone()
        parsed = timezone.make_aware(parsed, tzinfo)
    return raw_value[:64], parsed


def build_sale_temporal_fields(
    *,
    received_at,
    queue_session_id: str | None = '',
    session_seq_no=None,
    client_created_at_raw: str | None = '',
    client_monotonic_ms=None,
    anchor_operated_at=None,
    anchor_client_monotonic_ms=None,
    timezone_name: str | None = None,
    chronology_threshold_minutes: int = 15,
    monotonic_replay_window_hours: int = 12,
) -> dict:
    normalized_queue_session_id = normalize_queue_session_id(queue_session_id)
    normalized_session_seq_no = normalize_session_seq_no(session_seq_no)
    normalized_client_monotonic_ms = normalize_client_monotonic_ms(client_monotonic_ms)
    raw_client_created_at, parsed_client_created_at = parse_client_created_at_raw(
        client_created_at_raw,
        timezone_name=timezone_name,
    )

    operated_at_normalized = parsed_client_created_at or received_at
    chronology_estimated = False

    if normalized_queue_session_id and anchor_operated_at:
        if anchor_client_monotonic_ms is not None and normalized_client_monotonic_ms is not None:
            delta_ms = normalized_client_monotonic_ms - int(anchor_client_monotonic_ms)
            max_delta_ms = max(1, monotonic_replay_window_hours) * 60 * 60 * 1000
            if 0 <= delta_ms <= max_delta_ms:
                operated_at_normalized = anchor_operated_at + timedelta(milliseconds=delta_ms)
            else:
                chronology_estimated = True
                operated_at_normalized = parsed_client_created_at or received_at
        else:
            chronology_estimated = True
            operated_at_normalized = parsed_client_created_at or received_at

    if parsed_client_created_at:
        drift_seconds = abs((parsed_client_created_at - operated_at_normalized).total_seconds())
        if drift_seconds > max(0, chronology_threshold_minutes) * 60:
            chronology_estimated = True

    return {
        'queue_session_id': normalized_queue_session_id,
        'session_seq_no': normalized_session_seq_no,
        'client_created_at_raw': raw_client_created_at,
        'client_monotonic_ms': normalized_client_monotonic_ms,
        'operated_at_normalized': operated_at_normalized,
        'accounting_booked_at': received_at,
        'chronology_estimated': chronology_estimated,
    }
