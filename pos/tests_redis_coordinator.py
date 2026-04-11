"""Tests for the Redis-backed replay coordinator.

Uses mock Redis clients to simulate split-brain, zombie tickets,
heartbeat renewal, self-fencing, and Redis-down scenarios without
requiring a live Redis instance.
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from pos.infrastructure.replay_gateway.proxy import (
    ReplayCoordinatorTicket,
    ReplayGatewayAdmissionError,
)
from pos.infrastructure.replay_gateway.redis_coordinator import (
    FailClosedCoordinator,
    LUA_ADMIT,
    LUA_FENCE_CHECK,
    LUA_HEARTBEAT,
    LUA_RELEASE,
    RedisBucketedReplayCoordinator,
    RedisReplayCoordinatorTicket,
    TicketHeartbeatThread,
    build_replay_coordinator,
)


# ---------------------------------------------------------------------------
# Helpers: a mock Redis that executes Lua scripts against in-memory state
# ---------------------------------------------------------------------------

class _FakeRedisStore:
    """Minimal in-memory Redis simulator for coordinator script testing.

    Uses a controllable clock so tests never rely on ``time.sleep()``.
    """

    def __init__(self):
        self._strings: dict[str, tuple[str, float | None]] = {}
        self._sets: dict[str, set[str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()
        self._clock: float = 1000.0  # deterministic start

    # -- Fake clock -----------------------------------------------------------

    def now(self) -> float:
        return self._clock

    def advance(self, seconds: float) -> None:
        """Advance the deterministic clock."""
        self._clock += seconds

    # -- Expiry ---------------------------------------------------------------

    def _purge_if_expired(self, key: str) -> None:
        """Delete the key if its TTL has passed (caller holds lock)."""
        if key in self._strings:
            _, expire_at = self._strings[key]
            if expire_at is not None and self._clock > expire_at:
                del self._strings[key]

    # -- String commands ------------------------------------------------------

    def get(self, key: str) -> bytes | None:
        with self._lock:
            self._purge_if_expired(key)
            if key in self._strings:
                return self._strings[key][0].encode()
            return None

    def set(self, key: str, value: str, *, px: int | None = None) -> None:
        with self._lock:
            expire_at = (self._clock + px / 1000.0) if px else None
            self._strings[key] = (str(value), expire_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._strings.pop(key, None)

    def exists(self, key: str) -> int:
        with self._lock:
            self._purge_if_expired(key)
            return 1 if key in self._strings else 0

    def pexpire(self, key: str, px: int) -> int:
        with self._lock:
            if key in self._strings:
                val = self._strings[key][0]
                self._strings[key] = (val, self._clock + px / 1000.0)
                return 1
            return 0

    # -- Set commands ---------------------------------------------------------

    def sadd(self, key: str, *members: str) -> int:
        with self._lock:
            s = self._sets.setdefault(key, set())
            before = len(s)
            s.update(members)
            return len(s) - before

    def srem(self, key: str, *members: str) -> int:
        with self._lock:
            s = self._sets.get(key, set())
            before = len(s)
            s -= set(members)
            return before - len(s)

    def scard(self, key: str) -> int:
        with self._lock:
            return len(self._sets.get(key, set()))

    # -- Sorted-set commands --------------------------------------------------

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        with self._lock:
            z = self._zsets.setdefault(key, {})
            added = 0
            for member, score in mapping.items():
                if member not in z:
                    added += 1
                z[member] = score
            return added

    def zrem(self, key: str, *members: str) -> int:
        with self._lock:
            z = self._zsets.get(key, {})
            removed = 0
            for m in members:
                if m in z:
                    del z[m]
                    removed += 1
            return removed

    def zcard(self, key: str) -> int:
        with self._lock:
            return len(self._zsets.get(key, {}))

    def zremrangebyscore(self, key: str, _min_s, max_s: float) -> int:
        with self._lock:
            z = self._zsets.get(key, {})
            to_remove = [m for m, s in z.items() if s <= max_s]
            for m in to_remove:
                del z[m]
            return len(to_remove)

    def zrange(self, key: str, start: int, stop: int) -> list[bytes]:
        with self._lock:
            z = self._zsets.get(key, {})
            items = sorted(z.items(), key=lambda x: x[1])
            end = stop + 1 if stop >= 0 else len(items) + stop + 1
            return [m.encode() for m, _ in items[start:end]]

    def zscore(self, key: str, member: str) -> float | None:
        with self._lock:
            return self._zsets.get(key, {}).get(member)


class _FakeRegisteredScript:
    """Runs Lua-equivalent logic against ``_FakeRedisStore``."""

    def __init__(self, store: _FakeRedisStore, tag: str):
        self._store = store
        self._tag = tag

    def __call__(self, keys=None, args=None) -> bytes:
        keys = keys or []
        args = [str(a) for a in (args or [])]
        handler = {
            'admit': self._exec_admit,
            'release': self._exec_release,
            'heartbeat': self._exec_heartbeat,
            'fence_check': self._exec_fence_check,
        }.get(self._tag)
        if handler is None:
            raise ValueError(f'Unknown script tag: {self._tag}')
        return handler(keys, args)

    def _exec_admit(self, keys, argv):
        slots_key, ticket_key, waiters_key, slice_key = keys
        org_key, ticket_id = argv[0], argv[1]
        max_slots = int(argv[2])
        ticket_ttl_ms = int(argv[3])
        slice_seconds = float(argv[4])
        waiter_ttl_s = float(argv[5])
        now = float(argv[6])
        max_waiters = int(argv[7])
        slot_expiry = now + (ticket_ttl_ms / 1000.0)

        cutoff = now - waiter_ttl_s
        self._store.zremrangebyscore(waiters_key, '-inf', cutoff)
        self._store.zremrangebyscore(slots_key, '-inf', now)

        if self._store.exists(ticket_key):
            return json.dumps({'status': 'DENIED', 'reason': 'replay_organization_capacity_exhausted', 'scope': 'organization'}).encode()

        if self._store.zcard(slots_key) >= max_slots:
            if self._store.zcard(waiters_key) < max_waiters:
                self._store.zadd(waiters_key, {org_key: now})
            return json.dumps({'status': 'DENIED', 'reason': 'replay_cold_lane_capacity_exhausted', 'scope': 'cold_lane'}).encode()

        slice_raw = self._store.get(slice_key)
        if slice_raw is not None:
            if now - float(slice_raw) >= slice_seconds:
                waiter_count = self._store.zcard(waiters_key)
                own_waiter = self._store.zscore(waiters_key, org_key)
                has_other_waiters = False
                if own_waiter is not None:
                    has_other_waiters = waiter_count > 1
                else:
                    has_other_waiters = waiter_count > 0
                if has_other_waiters:
                    if self._store.zcard(waiters_key) < max_waiters:
                        self._store.zadd(waiters_key, {org_key: now})
                    return json.dumps({'status': 'DENIED', 'reason': 'replay_cold_lane_draining', 'scope': 'cold_lane'}).encode()
                self._store.set(slice_key, str(now))
        else:
            self._store.set(slice_key, str(now))

        self._store.zadd(slots_key, {org_key: slot_expiry})
        self._store.set(ticket_key, ticket_id, px=ticket_ttl_ms)
        self._store.zrem(waiters_key, org_key)
        safety = ticket_ttl_ms * 10
        self._store.pexpire(slice_key, safety)
        return json.dumps({'status': 'ADMITTED', 'reason': 'admitted', 'scope': 'bucket'}).encode()

    def _exec_release(self, keys, argv):
        slots_key, ticket_key = keys
        org_key, ticket_id = argv[0], argv[1]
        cur = self._store.get(ticket_key)
        if cur is not None and cur.decode() == ticket_id:
            self._store.delete(ticket_key)
            self._store.zrem(slots_key, org_key)
            return b'1'
        return b'0'

    def _exec_heartbeat(self, keys, argv):
        ticket_key, slots_key = keys[0], keys[1]
        ticket_id, ttl_ms, now, org_key = argv[0], int(argv[1]), float(argv[2]), argv[3]
        slot_expiry = now + (ttl_ms / 1000.0)
        cur = self._store.get(ticket_key)
        if cur is not None and cur.decode() == ticket_id:
            self._store.pexpire(ticket_key, ttl_ms)
            self._store.zadd(slots_key, {org_key: slot_expiry})
            return b'RENEWED'
        return b'EXPIRED'

    def _exec_fence_check(self, keys, argv):
        ticket_key = keys[0]
        ticket_id = argv[0]
        cur = self._store.get(ticket_key)
        if cur is not None and cur.decode() == ticket_id:
            return b'VALID'
        return b'FENCED'


_SCRIPT_TAG_MAP = {
    id(LUA_ADMIT): 'admit',
    id(LUA_RELEASE): 'release',
    id(LUA_HEARTBEAT): 'heartbeat',
    id(LUA_FENCE_CHECK): 'fence_check',
}


class _FakeRedisClient:
    """redis-py compatible client backed by ``_FakeRedisStore``."""

    def __init__(self, store: _FakeRedisStore | None = None):
        self._store = store or _FakeRedisStore()

    def register_script(self, script_text: str) -> _FakeRegisteredScript:
        # Match by content prefix (first 20 chars) since ``is`` may fail
        # across module boundaries.
        tag = _SCRIPT_TAG_MAP.get(id(script_text))
        if tag is None:
            # Fallback: match by content
            for lua_const, t in [
                (LUA_ADMIT, 'admit'),
                (LUA_RELEASE, 'release'),
                (LUA_HEARTBEAT, 'heartbeat'),
                (LUA_FENCE_CHECK, 'fence_check'),
            ]:
                if script_text == lua_const:
                    tag = t
                    break
        if tag is None:
            raise ValueError('Unknown Lua script')
        return _FakeRegisteredScript(self._store, tag)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class FailClosedCoordinatorTests(SimpleTestCase):

    def test_fail_closed_rejects_all_requests(self):
        coordinator = FailClosedCoordinator()
        with self.assertRaises(ReplayGatewayAdmissionError) as ctx:
            coordinator.admit(organization_key='org-x')
        self.assertEqual(ctx.exception.reason, 'replay_coordinator_unavailable')
        self.assertEqual(ctx.exception.scope, 'gateway')

    def test_fail_closed_release_is_noop(self):
        coordinator = FailClosedCoordinator()
        coordinator.release(None)
        coordinator.release(ReplayCoordinatorTicket(
            organization_key='org-x', bucket_index=0, bucket_key='{replay:b0}',
        ))


class RedisBucketedReplayCoordinatorTests(SimpleTestCase):

    def _make(self, *, store=None, max_slots=2, **kw):
        store = store or _FakeRedisStore()
        client = _FakeRedisClient(store)
        defaults = dict(
            redis_client=client, bucket_count=4, max_slots=max_slots,
            slice_seconds=120.0, waiter_ttl_seconds=30.0,
            ticket_ttl_ms=30_000,
            heartbeat_interval_seconds=300.0,
            max_waiters_per_bucket=100,
        )
        defaults.update(kw)
        return RedisBucketedReplayCoordinator(**defaults), store

    def test_admit_and_release_basic(self):
        coord, _ = self._make()
        ticket = coord.admit(organization_key='org-alpha')
        self.assertIsInstance(ticket, RedisReplayCoordinatorTicket)
        self.assertEqual(ticket.base_ticket.organization_key, 'org-alpha')
        coord.release(ticket)

    def test_same_org_cannot_double_admit(self):
        coord, _ = self._make(max_slots=5)
        t = coord.admit(organization_key='org-dup')
        with self.assertRaises(ReplayGatewayAdmissionError) as ctx:
            coord.admit(organization_key='org-dup')
        self.assertEqual(ctx.exception.reason, 'replay_organization_capacity_exhausted')
        coord.release(t)

    def test_split_brain_two_coordinators_same_redis(self):
        store = _FakeRedisStore()
        ca, _ = self._make(store=store, max_slots=5)
        cb, _ = self._make(store=store, max_slots=5)
        t = ca.admit(organization_key='org-x')
        with self.assertRaises(ReplayGatewayAdmissionError):
            cb.admit(organization_key='org-x')
        tb = cb.admit(organization_key='org-y')
        ca.release(t)
        cb.release(tb)

    def test_max_slots_enforced(self):
        coord, _ = self._make(max_slots=1, bucket_count=1)
        ta = coord.admit(organization_key='org-slot-a')
        with self.assertRaises(ReplayGatewayAdmissionError) as ctx:
            coord.admit(organization_key='org-slot-b')
        self.assertEqual(ctx.exception.reason, 'replay_cold_lane_capacity_exhausted')
        coord.release(ta)
        tb = coord.admit(organization_key='org-slot-b')
        coord.release(tb)

    def test_ticket_zombie_expires_via_ttl(self):
        """Ticket expires when the store clock advances past its TTL."""
        store = _FakeRedisStore()
        coord, _ = self._make(store=store, ticket_ttl_ms=5000)
        ticket = coord.admit(organization_key='org-zombie')
        # Immediately fails
        with self.assertRaises(ReplayGatewayAdmissionError):
            coord.admit(organization_key='org-zombie')
        # Advance clock past TTL (5s + margin)
        store.advance(6.0)
        # Now succeeds
        t2 = coord.admit(organization_key='org-zombie')
        self.assertIsInstance(t2, RedisReplayCoordinatorTicket)
        coord.release(t2)

    def test_fence_check_valid(self):
        coord, _ = self._make()
        t = coord.admit(organization_key='org-fence')
        self.assertTrue(coord.check_fence(t))
        coord.release(t)

    def test_fence_check_after_expiry(self):
        store = _FakeRedisStore()
        coord, _ = self._make(store=store, ticket_ttl_ms=5000)
        t = coord.admit(organization_key='org-fence-exp')
        store.advance(6.0)
        self.assertFalse(coord.check_fence(t))

    def test_redis_down_raises_admission_error(self):
        mock_client = MagicMock()
        mock_client.register_script.return_value = MagicMock(
            side_effect=ConnectionError('Redis unavailable'),
        )
        coord = RedisBucketedReplayCoordinator(
            redis_client=mock_client, bucket_count=4, max_slots=2,
            slice_seconds=120.0, waiter_ttl_seconds=30.0,
        )
        with self.assertRaises(ReplayGatewayAdmissionError) as ctx:
            coord.admit(organization_key='org-down')
        self.assertEqual(ctx.exception.reason, 'replay_coordinator_unavailable')
        self.assertEqual(ctx.exception.scope, 'gateway')

    def test_release_is_idempotent(self):
        coord, _ = self._make()
        t = coord.admit(organization_key='org-idem')
        coord.release(t)
        coord.release(t)  # no-op

    def test_release_none_is_noop(self):
        coord, _ = self._make()
        coord.release(None)

    def test_waiter_zset_cap_enforced(self):
        coord, _ = self._make(max_slots=1, bucket_count=1, max_waiters_per_bucket=2)
        t = coord.admit(organization_key='org-active')
        with self.assertRaises(ReplayGatewayAdmissionError):
            coord.admit(organization_key='org-w1')
        with self.assertRaises(ReplayGatewayAdmissionError):
            coord.admit(organization_key='org-w2')
        with self.assertRaises(ReplayGatewayAdmissionError) as ctx:
            coord.admit(organization_key='org-w3')
        self.assertEqual(ctx.exception.reason, 'replay_cold_lane_capacity_exhausted')
        coord.release(t)


class HeartbeatThreadTests(SimpleTestCase):

    def test_heartbeat_renews_ticket(self):
        store = _FakeRedisStore()
        client = _FakeRedisClient(store)
        script = client.register_script(LUA_HEARTBEAT)
        key = '{replay:b0}:org:hb:ticket'
        slots_key = '{replay:b0}:slots'
        tid = 'hb-ticket-1'
        org_key = 'hb'
        store.set(key, tid, px=500)

        hb = TicketHeartbeatThread(
            redis_client=client, ticket_key=key, slots_key=slots_key, ticket_id=tid, org_key=org_key,
            ticket_ttl_ms=500, interval_seconds=0.05,
            heartbeat_script=script,
        )
        hb.start()
        time.sleep(0.2)
        hb.stop()
        hb.join(timeout=1.0)
        self.assertFalse(hb.fenced)
        self.assertIsNotNone(store.get(key))

    def test_heartbeat_detects_stolen_ticket(self):
        store = _FakeRedisStore()
        client = _FakeRedisClient(store)
        script = client.register_script(LUA_HEARTBEAT)
        key = '{replay:b0}:org:stolen:ticket'
        slots_key = '{replay:b0}:slots'
        store.set(key, 'other-owner', px=60_000)

        hb = TicketHeartbeatThread(
            redis_client=client, ticket_key=key, slots_key=slots_key, ticket_id='my-ticket', org_key='stolen',
            ticket_ttl_ms=60_000, interval_seconds=0.02,
            heartbeat_script=script,
        )
        hb.start()
        time.sleep(0.1)
        hb.stop()
        hb.join(timeout=1.0)
        self.assertTrue(hb.fenced)

    def test_heartbeat_detects_deleted_key(self):
        store = _FakeRedisStore()
        client = _FakeRedisClient(store)
        script = client.register_script(LUA_HEARTBEAT)
        key = '{replay:b0}:org:del:ticket'
        slots_key = '{replay:b0}:slots'
        tid = 'del-ticket'
        store.set(key, tid, px=60_000)
        # Simulate key deletion (expired or released)
        store.delete(key)

        hb = TicketHeartbeatThread(
            redis_client=client, ticket_key=key, slots_key=slots_key, ticket_id=tid, org_key='del',
            ticket_ttl_ms=60_000, interval_seconds=0.02,
            heartbeat_script=script,
        )
        hb.start()
        time.sleep(0.1)
        hb.stop()
        hb.join(timeout=1.0)
        self.assertTrue(hb.fenced)


class BuildReplayCoordinatorFactoryTests(SimpleTestCase):

    def test_memory_dev_returns_in_memory(self):
        from pos.infrastructure.replay_gateway.proxy import InMemoryBucketedReplayCoordinator
        c = build_replay_coordinator(backend='memory_dev')
        self.assertIsInstance(c, InMemoryBucketedReplayCoordinator)

    def test_fail_closed_returns_fail_closed(self):
        self.assertIsInstance(build_replay_coordinator(backend='fail_closed'), FailClosedCoordinator)

    def test_no_redis_url_returns_fail_closed(self):
        self.assertIsInstance(build_replay_coordinator(backend='redis', redis_url=''), FailClosedCoordinator)

    def test_unknown_backend_returns_fail_closed(self):
        self.assertIsInstance(build_replay_coordinator(backend='???'), FailClosedCoordinator)
