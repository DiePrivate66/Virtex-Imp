from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone

from pos.domain.shared import parse_client_created_at_raw


REPLAY_TRUE_VALUES = {'1', 'true', 'yes', 'on'}


@dataclass
class ReplayAdmissionTicket:
    is_replay: bool = False
    lane: str = ''
    organization_id: int | None = None
    acquired_slot_keys: tuple[str, ...] = ()
    token: str = ''

    def attach_headers(self, response):
        if not self.is_replay:
            return response
        response['X-POS-Replay'] = '1'
        response['X-Bosco-Replay-Lane'] = self.lane or 'normal'
        return response

    def release(self) -> None:
        for key in self.acquired_slot_keys:
            if cache.get(key) == self.token:
                cache.delete(key)


class ReplayAdmissionError(Exception):
    def __init__(self, *, lane: str, scope: str, reason: str, retry_after: int):
        self.lane = lane
        self.scope = scope
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)

    def as_response(self):
        response = JsonResponse(
            {
                'status': 'error',
                'code': 'replay_backpressure',
                'mensaje': 'Sin capacidad inmediata para sincronizacion replay.',
                'scope': self.scope,
                'reason': self.reason,
                'lane': self.lane,
                'retry_after': self.retry_after,
            },
            status=429,
        )
        response['Retry-After'] = str(self.retry_after)
        response['X-POS-Replay'] = '1'
        response['X-Bosco-Replay-Lane'] = self.lane
        response['X-Bosco-Replay-Scope'] = self.scope
        response['X-Bosco-Replay-Reason'] = self.reason
        return response


def is_replay_request(header_value: str | None) -> bool:
    return str(header_value or '').strip().lower() in REPLAY_TRUE_VALUES


def admit_replay_request(
    *,
    replay_header: str | None,
    payload: dict | None,
    location=None,
    organization_id: int | None = None,
    received_at=None,
) -> ReplayAdmissionTicket:
    if not getattr(settings, 'POS_REPLAY_ADMISSION_ENABLED', False):
        return ReplayAdmissionTicket()
    if not is_replay_request(replay_header):
        return ReplayAdmissionTicket()

    received_at = received_at or timezone.now()
    payload = payload or {}
    timezone_name = getattr(location, 'timezone', None)
    _raw_client_created_at, parsed_client_created_at = parse_client_created_at_raw(
        payload.get('client_created_at_raw'),
        timezone_name=timezone_name,
    )
    lane = _resolve_replay_lane(
        received_at=received_at,
        parsed_client_created_at=parsed_client_created_at,
    )
    org_id = organization_id or getattr(location, 'organization_id', None)
    token = uuid4().hex
    slot_ttl_seconds = max(5, int(getattr(settings, 'POS_REPLAY_SLOT_TTL_SECONDS', 15)))
    retry_after = max(1, int(getattr(settings, 'POS_REPLAY_RETRY_AFTER_SECONDS', 5)))
    acquired_slot_keys: list[str] = []

    try:
        global_slot = _acquire_slot(
            prefix='pos:replay:global',
            slots=max(1, int(getattr(settings, 'POS_REPLAY_GLOBAL_SLOTS', 8))),
            ttl_seconds=slot_ttl_seconds,
            token=token,
        )
        if not global_slot:
            raise ReplayAdmissionError(
                lane=lane,
                scope='global',
                reason='replay_global_capacity_exhausted',
                retry_after=retry_after,
            )
        acquired_slot_keys.append(global_slot)

        org_slot = _acquire_slot(
            prefix=f'pos:replay:organization:{org_id or "unknown"}',
            slots=max(1, int(getattr(settings, 'POS_REPLAY_ORGANIZATION_SLOTS', 1))),
            ttl_seconds=slot_ttl_seconds,
            token=token,
        )
        if not org_slot:
            raise ReplayAdmissionError(
                lane=lane,
                scope='organization',
                reason='replay_organization_capacity_exhausted',
                retry_after=retry_after,
            )
        acquired_slot_keys.append(org_slot)

        if lane == 'cold':
            cold_slot = _acquire_slot(
                prefix='pos:replay:cold-lane',
                slots=max(1, int(getattr(settings, 'POS_REPLAY_COLD_LANE_SLOTS', 2))),
                ttl_seconds=slot_ttl_seconds,
                token=token,
            )
            if not cold_slot:
                raise ReplayAdmissionError(
                    lane=lane,
                    scope='cold_lane',
                    reason='replay_cold_lane_capacity_exhausted',
                    retry_after=retry_after,
                )
            acquired_slot_keys.append(cold_slot)
    except ReplayAdmissionError:
        _release_slots(acquired_slot_keys, token=token)
        raise

    return ReplayAdmissionTicket(
        is_replay=True,
        lane=lane,
        organization_id=org_id,
        acquired_slot_keys=tuple(acquired_slot_keys),
        token=token,
    )


def _resolve_replay_lane(*, received_at, parsed_client_created_at) -> str:
    if not parsed_client_created_at:
        return 'normal'
    cold_lane_hours = max(1, int(getattr(settings, 'POS_REPLAY_COLD_LANE_HOURS', 48)))
    if received_at - parsed_client_created_at > timedelta(hours=cold_lane_hours):
        return 'cold'
    return 'normal'


def _acquire_slot(*, prefix: str, slots: int, ttl_seconds: int, token: str) -> str | None:
    for slot in range(slots):
        namespace_key = f'{prefix}:{slot}'
        if cache.add(namespace_key, token, timeout=ttl_seconds):
            return namespace_key
    return None


def _release_slots(keys: list[str], *, token: str) -> None:
    for key in keys:
        if cache.get(key) == token:
            cache.delete(key)
