"""Redis-backed replay coordinator with fail-closed policy.

Provides distributed coordination for replay admission across multiple
gateway instances.  Uses Lua scripts for atomic operations, TTL-based
ticket expiration, heartbeat renewal, and self-fencing.

Designed to replace ``InMemoryBucketedReplayCoordinator`` in production.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from .proxy import (
    ReplayCoordinator,
    ReplayCoordinatorTicket,
    ReplayGatewayAdmissionError,
    stable_replay_bucket_index,
    stable_replay_bucket_key,
)

# ---------------------------------------------------------------------------
# Lua Scripts (idempotent, atomic)
# ---------------------------------------------------------------------------

# Keys used (all share hash tag {replay:b<bucket>} for Cluster affinity):
#   KEYS[1] = {replay:b<B>}:slots                  ZSET member=org_key score=slot lease expiry ts
#   KEYS[2] = {replay:b<B>}:org:<org_key>:ticket   STRING ticket_id, TTL
#   KEYS[3] = {replay:b<B>}:waiters                ZSET score=timestamp
#   KEYS[4] = {replay:b<B>}:slice:<org_key>        STRING slice start ts
#
# ARGV[1] = org_key
# ARGV[2] = ticket_id
# ARGV[3] = max_slots
# ARGV[4] = ticket_ttl_ms
# ARGV[5] = slice_seconds
# ARGV[6] = waiter_ttl_seconds
# ARGV[7] = now_seconds (float as string)
# ARGV[8] = max_waiters_per_bucket
#
# Returns: {"status": "ADMITTED|DENIED", "reason": "...", "scope": "..."}

LUA_ADMIT = """
local slots_key   = KEYS[1]
local ticket_key  = KEYS[2]
local waiters_key = KEYS[3]
local slice_key   = KEYS[4]

local org_key         = ARGV[1]
local ticket_id       = ARGV[2]
local max_slots       = tonumber(ARGV[3])
local ticket_ttl_ms   = tonumber(ARGV[4])
local slice_seconds   = tonumber(ARGV[5])
local waiter_ttl_s    = tonumber(ARGV[6])
local now             = tonumber(ARGV[7])
local max_waiters     = tonumber(ARGV[8])
local slot_expiry     = now + (ticket_ttl_ms / 1000.0)

-- Prune expired waiters first (bounded by max_waiters size)
local cutoff = now - waiter_ttl_s
redis.call('ZREMRANGEBYSCORE', waiters_key, '-inf', cutoff)
redis.call('ZREMRANGEBYSCORE', slots_key, '-inf', now)

-- 1. If this org already has an active ticket -> DENIED
if redis.call('EXISTS', ticket_key) == 1 then
    return cjson.encode({
        status = 'DENIED',
        reason = 'replay_organization_capacity_exhausted',
        scope  = 'organization',
    })
end

-- 2. If slots are full -> register as waiter (if under cap) -> DENIED
local active_count = redis.call('ZCARD', slots_key)
if active_count >= max_slots then
    local waiter_count = redis.call('ZCARD', waiters_key)
    if waiter_count < max_waiters then
        redis.call('ZADD', waiters_key, now, org_key)
    end
    return cjson.encode({
        status = 'DENIED',
        reason = 'replay_cold_lane_capacity_exhausted',
        scope  = 'cold_lane',
    })
end

-- 3. Draining check: if this org's slice expired and other orgs are waiting
local slice_start = redis.call('GET', slice_key)
if slice_start then
    local elapsed = now - tonumber(slice_start)
    if elapsed >= slice_seconds then
        -- Check if any OTHER org is waiting
        local waiter_count = redis.call('ZCARD', waiters_key)
        local own_waiter = redis.call('ZSCORE', waiters_key, org_key)
        local has_other_waiters = false
        if own_waiter then
            if waiter_count > 1 then
                has_other_waiters = true
            end
        elseif waiter_count > 0 then
            has_other_waiters = true
        end
        if has_other_waiters then
            local waiter_count = redis.call('ZCARD', waiters_key)
            if waiter_count < max_waiters then
                redis.call('ZADD', waiters_key, now, org_key)
            end
            return cjson.encode({
                status = 'DENIED',
                reason = 'replay_cold_lane_draining',
                scope  = 'cold_lane',
            })
        end
        -- Reset slice for this org
        redis.call('SET', slice_key, now)
    end
else
    redis.call('SET', slice_key, now)
end

-- 4. Admit: add to slots, create ticket with TTL, remove from waiters
redis.call('ZADD', slots_key, slot_expiry, org_key)
redis.call('SET', ticket_key, ticket_id, 'PX', ticket_ttl_ms)
redis.call('ZREM', waiters_key, org_key)

-- Set a safety TTL on the slots set and slice key so they don't leak
local safety_ttl = ticket_ttl_ms * 10
redis.call('PEXPIRE', slots_key, safety_ttl)
redis.call('PEXPIRE', slice_key, safety_ttl)

return cjson.encode({
    status = 'ADMITTED',
    reason = 'admitted',
    scope  = 'bucket',
})
"""

LUA_RELEASE = """
local slots_key  = KEYS[1]
local ticket_key = KEYS[2]

local org_key   = ARGV[1]
local ticket_id = ARGV[2]

-- Only release if the ticket belongs to this caller (idempotent)
local current = redis.call('GET', ticket_key)
if current == ticket_id then
    redis.call('DEL', ticket_key)
    redis.call('ZREM', slots_key, org_key)
    return 1
end
return 0
"""

LUA_HEARTBEAT = """
local ticket_key = KEYS[1]
local slots_key  = KEYS[2]

local ticket_id    = ARGV[1]
local ticket_ttl_ms = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local org_key = ARGV[4]
local slot_expiry = now + (ticket_ttl_ms / 1000.0)

local current = redis.call('GET', ticket_key)
if current == ticket_id then
    redis.call('PEXPIRE', ticket_key, ticket_ttl_ms)
    redis.call('ZADD', slots_key, slot_expiry, org_key)
    return 'RENEWED'
end
return 'EXPIRED'
"""

LUA_FENCE_CHECK = """
local ticket_key = KEYS[1]
local ticket_id  = ARGV[1]

local current = redis.call('GET', ticket_key)
if current == ticket_id then
    return 'VALID'
end
return 'FENCED'
"""


# ---------------------------------------------------------------------------
# Heartbeat Thread
# ---------------------------------------------------------------------------

class TicketHeartbeatThread(threading.Thread):
    """Renews a Redis ticket TTL periodically until stopped.

    If a renewal fails or the ticket is no longer ours, sets ``fenced``
    so the proxy can abort via self-fencing.
    """

    daemon = True

    def __init__(
        self,
        *,
        redis_client,
        ticket_key: str,
        slots_key: str,
        ticket_id: str,
        org_key: str,
        ticket_ttl_ms: int,
        interval_seconds: float,
        heartbeat_script,
    ):
        super().__init__()
        self._redis = redis_client
        self._ticket_key = ticket_key
        self._slots_key = slots_key
        self._ticket_id = ticket_id
        self._org_key = org_key
        self._ticket_ttl_ms = ticket_ttl_ms
        self._interval = max(0.01, float(interval_seconds))
        self._heartbeat_script = heartbeat_script
        self._stop_event = threading.Event()
        self._fenced = False
        self._lock = threading.Lock()

    @property
    def fenced(self) -> bool:
        with self._lock:
            return self._fenced

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(timeout=self._interval):
            try:
                result = self._heartbeat_script(
                    keys=[self._ticket_key, self._slots_key],
                    args=[self._ticket_id, self._ticket_ttl_ms, f'{time.time():.3f}', self._org_key],
                )
                decoded = result.decode() if isinstance(result, bytes) else str(result)
                if decoded != 'RENEWED':
                    with self._lock:
                        self._fenced = True
                    return
            except Exception:
                with self._lock:
                    self._fenced = True
                return


# ---------------------------------------------------------------------------
# Extended Ticket (carries heartbeat + fencing metadata)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RedisReplayCoordinatorTicket:
    """Wraps the base ticket with Redis-specific metadata."""
    base_ticket: ReplayCoordinatorTicket
    ticket_id: str
    ticket_key: str
    heartbeat: TicketHeartbeatThread | None = None


# ---------------------------------------------------------------------------
# Fail-Closed Coordinator
# ---------------------------------------------------------------------------

class FailClosedCoordinator(ReplayCoordinator):
    """Rejects all replay traffic.  Used when Redis is unavailable
    and the policy is ``fail_closed``."""

    def admit(self, *, organization_key: str) -> ReplayCoordinatorTicket:
        raise ReplayGatewayAdmissionError(
            lane='cold',
            scope='gateway',
            reason='replay_coordinator_unavailable',
        )

    def release(self, ticket: ReplayCoordinatorTicket | None) -> None:
        pass


# ---------------------------------------------------------------------------
# Redis Bucketed Replay Coordinator
# ---------------------------------------------------------------------------

class RedisBucketedReplayCoordinator(ReplayCoordinator):
    """Distributed replay coordinator backed by Redis.

    Fail-closed: if Redis is unreachable, admission is denied outright.
    Tickets carry a short TTL and are renewed by a heartbeat thread.
    The proxy must self-fence before processing each upstream chunk.
    """

    def __init__(
        self,
        *,
        redis_client,
        bucket_count: int,
        max_slots: int,
        slice_seconds: float,
        waiter_ttl_seconds: float,
        ticket_ttl_ms: int = 30_000,
        heartbeat_interval_seconds: float = 10.0,
        max_waiters_per_bucket: int = 100,
    ):
        self._redis = redis_client
        self._bucket_count = max(1, int(bucket_count))
        self._max_slots = max(1, int(max_slots))
        self._slice_seconds = max(0.1, float(slice_seconds))
        self._waiter_ttl_seconds = max(0.1, float(waiter_ttl_seconds))
        self._ticket_ttl_ms = max(1000, int(ticket_ttl_ms))
        self._heartbeat_interval = max(0.01, float(heartbeat_interval_seconds))
        self._max_waiters = max(1, int(max_waiters_per_bucket))

        # Register Lua scripts
        self._admit_script = self._redis.register_script(LUA_ADMIT)
        self._release_script = self._redis.register_script(LUA_RELEASE)
        self._heartbeat_script = self._redis.register_script(LUA_HEARTBEAT)
        self._fence_check_script = self._redis.register_script(LUA_FENCE_CHECK)

    def _key_prefix(self, bucket_index: int) -> str:
        return f'{{replay:b{bucket_index}}}'

    def _keys_for(self, bucket_index: int, org_key: str) -> tuple[str, str, str, str]:
        prefix = self._key_prefix(bucket_index)
        return (
            f'{prefix}:slots',
            f'{prefix}:org:{org_key}:ticket',
            f'{prefix}:waiters',
            f'{prefix}:slice:{org_key}',
        )

    def admit(self, *, organization_key: str) -> ReplayCoordinatorTicket:
        org_key = (organization_key or 'unknown').strip() or 'unknown'
        bucket_index = stable_replay_bucket_index(
            organization_key=org_key,
            bucket_count=self._bucket_count,
        )
        bucket_key = stable_replay_bucket_key(
            organization_key=org_key,
            bucket_count=self._bucket_count,
        )
        ticket_id = uuid.uuid4().hex

        slots_key, ticket_key, waiters_key, slice_key = self._keys_for(bucket_index, org_key)

        try:
            import json as _json
            raw_result = self._admit_script(
                keys=[slots_key, ticket_key, waiters_key, slice_key],
                args=[
                    org_key,
                    ticket_id,
                    self._max_slots,
                    self._ticket_ttl_ms,
                    self._slice_seconds,
                    self._waiter_ttl_seconds,
                    f'{time.time():.3f}',
                    self._max_waiters,
                ],
            )
            result = _json.loads(raw_result)
        except Exception as exc:
            # Redis unreachable or script error -> fail closed
            raise ReplayGatewayAdmissionError(
                lane='cold',
                scope='gateway',
                reason='replay_coordinator_unavailable',
            ) from exc

        if result.get('status') != 'ADMITTED':
            raise ReplayGatewayAdmissionError(
                lane='cold',
                scope=result.get('scope', 'unknown'),
                reason=result.get('reason', 'unknown'),
            )

        # Start heartbeat thread
        heartbeat = TicketHeartbeatThread(
            redis_client=self._redis,
            ticket_key=ticket_key,
            slots_key=slots_key,
            ticket_id=ticket_id,
            org_key=org_key,
            ticket_ttl_ms=self._ticket_ttl_ms,
            interval_seconds=self._heartbeat_interval,
            heartbeat_script=self._heartbeat_script,
        )
        heartbeat.start()

        base_ticket = ReplayCoordinatorTicket(
            organization_key=org_key,
            bucket_index=bucket_index,
            bucket_key=bucket_key,
        )

        return RedisReplayCoordinatorTicket(
            base_ticket=base_ticket,
            ticket_id=ticket_id,
            ticket_key=ticket_key,
            heartbeat=heartbeat,
        )

    def release(self, ticket: ReplayCoordinatorTicket | None) -> None:
        if ticket is None:
            return

        # Handle both raw ReplayCoordinatorTicket and our Redis wrapper
        if isinstance(ticket, RedisReplayCoordinatorTicket):
            redis_ticket = ticket
        else:
            return

        # Stop heartbeat first
        if redis_ticket.heartbeat is not None:
            redis_ticket.heartbeat.stop()

        org_key = redis_ticket.base_ticket.organization_key
        bucket_index = redis_ticket.base_ticket.bucket_index
        slots_key = f'{self._key_prefix(bucket_index)}:slots'
        ticket_key = redis_ticket.ticket_key

        try:
            self._release_script(
                keys=[slots_key, ticket_key],
                args=[org_key, redis_ticket.ticket_id],
            )
        except Exception:
            # Best effort: ticket will expire via TTL anyway
            pass

    def check_fence(self, ticket) -> bool:
        """Return True if the ticket is still valid in Redis.

        Used for self-fencing: the proxy calls this before processing
        each upstream chunk.  If the heartbeat drifted and the ticket
        expired, this returns False and the proxy must abort.
        """
        if not isinstance(ticket, RedisReplayCoordinatorTicket):
            return True

        # Fast path: check heartbeat thread flag first (no Redis call)
        if ticket.heartbeat is not None and ticket.heartbeat.fenced:
            return False

        try:
            result = self._fence_check_script(
                keys=[ticket.ticket_key],
                args=[ticket.ticket_id],
            )
            decoded = result.decode() if isinstance(result, bytes) else str(result)
            return decoded == 'VALID'
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_replay_coordinator(
    *,
    backend: str,
    redis_url: str = '',
    bucket_count: int = 8,
    max_slots: int = 2,
    slice_seconds: float = 120.0,
    waiter_ttl_seconds: float = 30.0,
    ticket_ttl_ms: int = 30_000,
    heartbeat_interval_seconds: float = 10.0,
    max_waiters_per_bucket: int = 100,
) -> ReplayCoordinator:
    """Build a replay coordinator from configuration.

    ``backend`` must be one of:
    - ``redis``       — distributed via Redis (production)
    - ``memory_dev``  — in-process (development only)
    - ``fail_closed`` — reject all replay traffic
    """
    from .proxy import InMemoryBucketedReplayCoordinator

    if backend == 'memory_dev':
        return InMemoryBucketedReplayCoordinator(
            bucket_count=bucket_count,
            slots=max_slots,
            slice_seconds=slice_seconds,
            waiter_ttl_seconds=waiter_ttl_seconds,
        )

    if backend == 'fail_closed' or not redis_url:
        return FailClosedCoordinator()

    if backend == 'redis':
        import redis as redis_lib
        client = redis_lib.Redis.from_url(redis_url, decode_responses=False)
        return RedisBucketedReplayCoordinator(
            redis_client=client,
            bucket_count=bucket_count,
            max_slots=max_slots,
            slice_seconds=slice_seconds,
            waiter_ttl_seconds=waiter_ttl_seconds,
            ticket_ttl_ms=ticket_ttl_ms,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            max_waiters_per_bucket=max_waiters_per_bucket,
        )

    return FailClosedCoordinator()
