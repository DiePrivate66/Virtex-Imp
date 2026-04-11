from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

from django.test import SimpleTestCase

from pos.infrastructure.replay_gateway import (
    ReplayGatewayConfig,
    build_gateway_server,
    stable_replay_bucket_index,
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
