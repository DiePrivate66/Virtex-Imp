from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from unittest import mock

from django.test import SimpleTestCase

from pos.infrastructure.replay_gateway import (
    ReplayGatewayConfig,
    RedisBucketedReplayCoordinator,
    build_gateway_server,
    stable_replay_bucket_index,
)
from pos.infrastructure.replay_gateway.proxy import ReplayGatewayAdmissionError
from pos.infrastructure.replay_gateway.redis_coordinator import (
    LUA_ADMIT,
    LUA_FENCE_CHECK,
    LUA_HEARTBEAT,
    LUA_RELEASE,
)


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


class _StubUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    hold_started_event = threading.Event()

    def log_message(self, format, *args):
        return

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        content_length = int(self.headers.get('Content-Length') or 0)
        if content_length:
            self.rfile.read(content_length)

        if self.path == '/registrar_venta/fast':
            body = json.dumps({'status': 'ok'}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self._safe_write(body)
            return

        if self.path == '/registrar_venta/slow-first-byte':
            time.sleep(0.8)
            body = json.dumps({'status': 'late'}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self._safe_write(body)
            return

        if self.path == '/registrar_venta/idle-stream':
            part_one = b'{"status":"'
            part_two = b'stream"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(part_one) + len(part_two)))
            self.end_headers()
            self._safe_write(part_one)
            time.sleep(0.8)
            self._safe_write(part_two)
            return

        if self.path == '/registrar_venta/hold-first-byte':
            type(self).hold_started_event.set()
            time.sleep(0.4)
            body = json.dumps({'status': 'held'}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self._safe_write(body)
            return

        if self.path == '/health/slow':
            time.sleep(0.8)
            body = b'OK'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self._safe_write(body)
            return

        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _safe_write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return


class ReplayGatewayProxyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.upstream_port = _get_free_port()
        cls.upstream_server = ThreadingHTTPServer(('127.0.0.1', cls.upstream_port), _StubUpstreamHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream_server.serve_forever, daemon=True)
        cls.upstream_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream_server.shutdown()
        cls.upstream_thread.join(timeout=5)
        cls.upstream_server.server_close()
        super().tearDownClass()

    def _start_gateway(
        self,
        *,
        total_timeout: float,
        idle_timeout: float,
        cold_lane_slots: int = 2,
        cold_lane_hours: int = 48,
        cold_slice_seconds: float = 120.0,
        waiter_ttl_seconds: float = 30.0,
        bucket_count: int = 8,
    ):
        gateway_port = _get_free_port()
        config = ReplayGatewayConfig(
            bind_host='127.0.0.1',
            bind_port=gateway_port,
            upstream_host='127.0.0.1',
            upstream_port=self.upstream_port,
            replay_paths=('/registrar_venta/', '/api/reconciliar-pago/'),
            replay_total_timeout_seconds=total_timeout,
            replay_idle_timeout_seconds=idle_timeout,
            upstream_timeout_seconds=3.0,
            retry_after_seconds=5,
            replay_cold_lane_hours=cold_lane_hours,
            replay_cold_lane_slots=cold_lane_slots,
            replay_cold_slice_seconds=cold_slice_seconds,
            replay_waiter_ttl_seconds=waiter_ttl_seconds,
            replay_bucket_count=bucket_count,
        )
        gateway_server = build_gateway_server(config)
        gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
        gateway_thread.start()

        def _stop_gateway():
            gateway_server.shutdown()
            gateway_thread.join(timeout=5)
            gateway_server.server_close()

        self.addCleanup(_stop_gateway)
        return gateway_port

    def _replay_headers(self, *, organization: str, old: bool = True) -> tuple[dict[str, str], bytes]:
        body_timestamp = '2026-01-01T00:00:00+00:00' if old else '2026-04-04T00:00:00+00:00'
        return {
            'Content-Type': 'application/json',
            'X-POS-Replay': '1',
            'X-Bosco-Replay-Organization': organization,
        }, json.dumps({'client_created_at_raw': body_timestamp}).encode('utf-8')

    def test_replay_gateway_returns_success_for_fast_replay_request(self):
        gateway_port = self._start_gateway(total_timeout=1.0, idle_timeout=0.5)
        headers, body = self._replay_headers(organization='org-fast')
        connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        connection.request('POST', '/registrar_venta/fast', body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode('utf-8'))

        self.assertEqual(response.status, 200)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(response.getheader('X-Bosco-Replay-Gateway'), 'active')
        self.assertEqual(response.getheader('X-Bosco-Replay-Lane'), 'cold')
        self.assertTrue(str(response.getheader('X-Bosco-Replay-Bucket') or '').startswith('{replay:b'))

    def test_replay_gateway_enforces_idle_timeout_before_first_byte(self):
        gateway_port = self._start_gateway(total_timeout=2.0, idle_timeout=0.2)
        headers, body = self._replay_headers(organization='org-idle')
        connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        connection.request('POST', '/registrar_venta/slow-first-byte', body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode('utf-8'))

        self.assertEqual(response.status, 504)
        self.assertEqual(payload['code'], 'replay_gateway_idle_timeout')
        self.assertEqual(payload['lane'], 'cold')
        self.assertEqual(response.getheader('X-Bosco-Replay-Scope'), 'gateway')

    def test_replay_gateway_enforces_total_timeout(self):
        gateway_port = self._start_gateway(total_timeout=0.2, idle_timeout=1.0)
        headers, body = self._replay_headers(organization='org-total')
        connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        connection.request('POST', '/registrar_venta/slow-first-byte', body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode('utf-8'))

        self.assertEqual(response.status, 504)
        self.assertEqual(payload['code'], 'replay_gateway_total_timeout')

    def test_non_replay_request_bypasses_replay_timeout_policy(self):
        gateway_port = self._start_gateway(total_timeout=0.2, idle_timeout=0.2)
        connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        connection.request('GET', '/health/slow')
        response = connection.getresponse()
        body = response.read().decode('utf-8')

        self.assertEqual(response.status, 200)
        self.assertEqual(body, 'OK')

    def test_replay_gateway_limits_one_cold_request_per_organization(self):
        gateway_port = self._start_gateway(
            total_timeout=2.0,
            idle_timeout=1.0,
            cold_lane_slots=1,
            cold_slice_seconds=1.0,
        )
        _StubUpstreamHandler.hold_started_event.clear()
        first_headers, first_body = self._replay_headers(organization='org-busy')
        response_holder = {}

        def _first_request():
            connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
            connection.request('POST', '/registrar_venta/hold-first-byte', body=first_body, headers=first_headers)
            response = connection.getresponse()
            response_holder['status'] = response.status
            response_holder['payload'] = json.loads(response.read().decode('utf-8'))

        first_thread = threading.Thread(target=_first_request, daemon=True)
        first_thread.start()
        self.assertTrue(_StubUpstreamHandler.hold_started_event.wait(timeout=1.0))

        second_headers, second_body = self._replay_headers(organization='org-busy')
        second_connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        second_connection.request('POST', '/registrar_venta/fast', body=second_body, headers=second_headers)
        second_response = second_connection.getresponse()
        second_payload = json.loads(second_response.read().decode('utf-8'))

        first_thread.join(timeout=2.0)
        self.assertEqual(response_holder.get('status'), 200)
        self.assertEqual(second_response.status, 429)
        self.assertEqual(second_payload['reason'], 'replay_organization_capacity_exhausted')
        self.assertEqual(second_response.getheader('X-Bosco-Replay-Scope'), 'organization')

    def test_replay_gateway_drains_expired_cold_slice_after_current_batch(self):
        gateway_port = self._start_gateway(
            total_timeout=2.0,
            idle_timeout=1.0,
            cold_lane_slots=1,
            cold_slice_seconds=0.1,
            waiter_ttl_seconds=2.0,
            bucket_count=1,
        )
        _StubUpstreamHandler.hold_started_event.clear()
        first_headers, first_body = self._replay_headers(organization='org-a')
        first_result = {}

        def _first_request():
            connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
            connection.request('POST', '/registrar_venta/hold-first-byte', body=first_body, headers=first_headers)
            response = connection.getresponse()
            first_result['status'] = response.status
            first_result['payload'] = json.loads(response.read().decode('utf-8'))

        first_thread = threading.Thread(target=_first_request, daemon=True)
        first_thread.start()
        self.assertTrue(_StubUpstreamHandler.hold_started_event.wait(timeout=1.0))
        time.sleep(0.15)

        waiter_headers, waiter_body = self._replay_headers(organization='org-b')
        waiter_connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        waiter_connection.request('POST', '/registrar_venta/fast', body=waiter_body, headers=waiter_headers)
        waiter_response = waiter_connection.getresponse()
        waiter_payload = json.loads(waiter_response.read().decode('utf-8'))

        first_thread.join(timeout=2.0)
        self.assertEqual(first_result.get('status'), 200)
        self.assertEqual(waiter_response.status, 429)
        self.assertEqual(waiter_payload['reason'], 'replay_cold_lane_capacity_exhausted')

        drain_headers, drain_body = self._replay_headers(organization='org-a')
        drain_connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        drain_connection.request('POST', '/registrar_venta/fast', body=drain_body, headers=drain_headers)
        drain_response = drain_connection.getresponse()
        drain_payload = json.loads(drain_response.read().decode('utf-8'))

        self.assertEqual(drain_response.status, 429)
        self.assertEqual(drain_payload['reason'], 'replay_cold_lane_draining')
        self.assertEqual(drain_response.getheader('X-Bosco-Replay-Gateway'), 'draining')

        winner_connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        winner_connection.request('POST', '/registrar_venta/fast', body=waiter_body, headers=waiter_headers)
        winner_response = winner_connection.getresponse()
        winner_payload = json.loads(winner_response.read().decode('utf-8'))

        self.assertEqual(winner_response.status, 200)
        self.assertEqual(winner_payload['status'], 'ok')

    def test_replay_gateway_isolates_cold_lane_capacity_by_bucket(self):
        bucket_count = 2
        org_a = 'org-bucket-a'
        org_b = 'org-bucket-b'
        while stable_replay_bucket_index(organization_key=org_b, bucket_count=bucket_count) == stable_replay_bucket_index(
            organization_key=org_a,
            bucket_count=bucket_count,
        ):
            org_b += '-x'

        gateway_port = self._start_gateway(
            total_timeout=2.0,
            idle_timeout=1.0,
            cold_lane_slots=1,
            cold_slice_seconds=1.0,
            bucket_count=bucket_count,
        )
        _StubUpstreamHandler.hold_started_event.clear()
        first_headers, first_body = self._replay_headers(organization=org_a)
        first_result = {}

        def _first_request():
            connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
            connection.request('POST', '/registrar_venta/hold-first-byte', body=first_body, headers=first_headers)
            response = connection.getresponse()
            first_result['status'] = response.status
            first_result['payload'] = json.loads(response.read().decode('utf-8'))

        first_thread = threading.Thread(target=_first_request, daemon=True)
        first_thread.start()
        self.assertTrue(_StubUpstreamHandler.hold_started_event.wait(timeout=1.0))

        second_headers, second_body = self._replay_headers(organization=org_b)
        second_connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        second_connection.request('POST', '/registrar_venta/fast', body=second_body, headers=second_headers)
        second_response = second_connection.getresponse()
        second_payload = json.loads(second_response.read().decode('utf-8'))

        first_thread.join(timeout=2.0)
        self.assertEqual(first_result.get('status'), 200)
        self.assertEqual(second_response.status, 200)
        self.assertEqual(second_payload['status'], 'ok')


class _FakeRedis:
    def __init__(self):
        self.now = 0.0
        self._strings: dict[str, str] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._key_expiry: dict[str, float] = {}

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)
        self._prune_all()

    def register_script(self, source: str):
        if source == LUA_ADMIT:
            return self._lua_admit
        if source == LUA_RELEASE:
            return self._lua_release
        if source == LUA_HEARTBEAT:
            return self._lua_heartbeat
        if source == LUA_FENCE_CHECK:
            return self._lua_fence_check
        raise AssertionError('unknown lua script')

    def _prune_all(self):
        for key in list(self._key_expiry.keys()):
            self._prune_key(key)

    def _prune_key(self, key: str):
        expiry = self._key_expiry.get(key)
        if expiry is not None and self.now >= expiry:
            self._key_expiry.pop(key, None)
            self._strings.pop(key, None)
            self._zsets.pop(key, None)

    def _get_string(self, key: str) -> str | None:
        self._prune_key(key)
        return self._strings.get(key)

    def _set_string(self, key: str, value: str):
        self._prune_key(key)
        self._strings[key] = value

    def _get_zset(self, key: str) -> dict[str, float]:
        self._prune_key(key)
        return self._zsets.setdefault(key, {})

    def _zremrangebyscore(self, key: str, min_score: float, max_score: float):
        zset = self._get_zset(key)
        for member, score in list(zset.items()):
            if min_score <= score <= max_score:
                zset.pop(member, None)

    def _lua_admit(self, *, keys, args):
        slots_key, ticket_key, waiters_key, slice_key = keys
        org_key = str(args[0])
        ticket_id = str(args[1])
        max_slots = int(args[2])
        ticket_ttl_ms = int(args[3])
        slice_seconds = float(args[4])
        waiter_ttl_s = float(args[5])
        now = float(args[6])
        max_waiters = int(args[7])
        self.now = max(self.now, now)
        slot_expiry = now + (ticket_ttl_ms / 1000.0)

        cutoff = now - waiter_ttl_s
        self._zremrangebyscore(waiters_key, float('-inf'), cutoff)
        self._zremrangebyscore(slots_key, float('-inf'), now)

        if self._get_string(ticket_key) is not None:
            return json.dumps({
                'status': 'DENIED',
                'reason': 'replay_organization_capacity_exhausted',
                'scope': 'organization',
            })

        active_count = len(self._get_zset(slots_key))
        if active_count >= max_slots:
            waiters = self._get_zset(waiters_key)
            if len(waiters) < max_waiters:
                waiters[org_key] = now
            return json.dumps({
                'status': 'DENIED',
                'reason': 'replay_cold_lane_capacity_exhausted',
                'scope': 'cold_lane',
            })

        slice_start = self._get_string(slice_key)
        if slice_start is not None:
            elapsed = now - float(slice_start)
            if elapsed >= slice_seconds:
                waiters = self._get_zset(waiters_key)
                waiter_count = len(waiters)
                own_waiter = waiters.get(org_key)
                has_other_waiters = False
                if own_waiter is not None:
                    if waiter_count > 1:
                        has_other_waiters = True
                elif waiter_count > 0:
                    has_other_waiters = True
                if has_other_waiters:
                    if len(waiters) < max_waiters or org_key in waiters:
                        waiters[org_key] = now
                    return json.dumps({
                        'status': 'DENIED',
                        'reason': 'replay_cold_lane_draining',
                        'scope': 'cold_lane',
                    })
                self._set_string(slice_key, str(now))
        else:
            self._set_string(slice_key, str(now))

        self._get_zset(slots_key)[org_key] = slot_expiry
        self._set_string(ticket_key, ticket_id)
        self._key_expiry[ticket_key] = now + (ticket_ttl_ms / 1000.0)
        self._get_zset(waiters_key).pop(org_key, None)

        safety_ttl = (ticket_ttl_ms * 10) / 1000.0
        self._key_expiry[slots_key] = now + safety_ttl
        self._key_expiry[slice_key] = now + safety_ttl
        return json.dumps({
            'status': 'ADMITTED',
            'reason': 'admitted',
            'scope': 'bucket',
        })

    def _lua_release(self, *, keys, args):
        slots_key, ticket_key = keys
        org_key = str(args[0])
        ticket_id = str(args[1])
        current = self._get_string(ticket_key)
        if current == ticket_id:
            self._strings.pop(ticket_key, None)
            self._key_expiry.pop(ticket_key, None)
            self._get_zset(slots_key).pop(org_key, None)
            return 1
        return 0

    def _lua_heartbeat(self, *, keys, args):
        ticket_key, slots_key = keys
        ticket_id = str(args[0])
        ticket_ttl_ms = int(args[1])
        now = float(args[2])
        org_key = str(args[3])
        self.now = max(self.now, now)
        current = self._get_string(ticket_key)
        if current == ticket_id:
            expiry = now + (ticket_ttl_ms / 1000.0)
            self._key_expiry[ticket_key] = expiry
            self._get_zset(slots_key)[org_key] = expiry
            return 'RENEWED'
        return 'EXPIRED'

    def _lua_fence_check(self, *, keys, args):
        ticket_key = keys[0]
        ticket_id = str(args[0])
        current = self._get_string(ticket_key)
        if current == ticket_id:
            return 'VALID'
        return 'FENCED'


class RedisReplayCoordinatorTests(SimpleTestCase):
    def test_redis_replay_coordinator_prunes_expired_slot_leases(self):
        fake_redis = _FakeRedis()
        coordinator = RedisBucketedReplayCoordinator(
            redis_client=fake_redis,
            bucket_count=1,
            max_slots=1,
            slice_seconds=120.0,
            waiter_ttl_seconds=30.0,
            ticket_ttl_ms=1000,
            heartbeat_interval_seconds=60.0,
        )

        with mock.patch('pos.infrastructure.replay_gateway.redis_coordinator.time.time', return_value=0.0):
            ticket_a = coordinator.admit(organization_key='org-a')
        ticket_a.heartbeat.stop()
        fake_redis.advance(1.1)

        with mock.patch('pos.infrastructure.replay_gateway.redis_coordinator.time.time', return_value=1.1):
            ticket_b = coordinator.admit(organization_key='org-b')
        ticket_b.heartbeat.stop()

        self.assertEqual(ticket_a.base_ticket.organization_key, 'org-a')
        self.assertEqual(ticket_b.base_ticket.organization_key, 'org-b')

    def test_redis_replay_coordinator_denies_when_fenced(self):
        fake_redis = _FakeRedis()
        coordinator = RedisBucketedReplayCoordinator(
            redis_client=fake_redis,
            bucket_count=1,
            max_slots=1,
            slice_seconds=120.0,
            waiter_ttl_seconds=30.0,
            ticket_ttl_ms=1000,
            heartbeat_interval_seconds=60.0,
        )

        with mock.patch('pos.infrastructure.replay_gateway.redis_coordinator.time.time', return_value=0.0):
            ticket = coordinator.admit(organization_key='org-a')
        ticket.heartbeat.stop()
        fake_redis.advance(1.1)

        self.assertFalse(coordinator.check_fence(ticket))

    def test_redis_replay_coordinator_uses_draining_when_other_waiter_exists(self):
        fake_redis = _FakeRedis()
        coordinator = RedisBucketedReplayCoordinator(
            redis_client=fake_redis,
            bucket_count=1,
            max_slots=1,
            slice_seconds=0.1,
            waiter_ttl_seconds=30.0,
            ticket_ttl_ms=30_000,
            heartbeat_interval_seconds=60.0,
        )

        with mock.patch('pos.infrastructure.replay_gateway.redis_coordinator.time.time', side_effect=[0.0, 0.2, 0.2]):
            ticket_a = coordinator.admit(organization_key='org-a')
            with self.assertRaises(ReplayGatewayAdmissionError) as waiter_exc:
                coordinator.admit(organization_key='org-b')
            self.assertEqual(waiter_exc.exception.reason, 'replay_cold_lane_capacity_exhausted')

            coordinator.release(ticket_a)

            with self.assertRaises(ReplayGatewayAdmissionError) as draining_exc:
                coordinator.admit(organization_key='org-a')

        self.assertEqual(draining_exc.exception.reason, 'replay_cold_lane_draining')
