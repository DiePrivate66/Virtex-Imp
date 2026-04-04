from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

from django.test import SimpleTestCase

from pos.infrastructure.replay_gateway import ReplayGatewayConfig, build_gateway_server


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


class _StubUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

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

    def _start_gateway(self, *, total_timeout: float, idle_timeout: float):
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

    def test_replay_gateway_returns_success_for_fast_replay_request(self):
        gateway_port = self._start_gateway(total_timeout=1.0, idle_timeout=0.5)
        connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        connection.request(
            'POST',
            '/registrar_venta/fast',
            body=b'{}',
            headers={
                'Content-Type': 'application/json',
                'Content-Length': '2',
                'X-POS-Replay': '1',
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode('utf-8'))

        self.assertEqual(response.status, 200)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(response.getheader('X-Bosco-Replay-Gateway'), 'active')

    def test_replay_gateway_enforces_idle_timeout_before_first_byte(self):
        gateway_port = self._start_gateway(total_timeout=2.0, idle_timeout=0.2)
        connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        connection.request(
            'POST',
            '/registrar_venta/slow-first-byte',
            body=b'{}',
            headers={
                'Content-Type': 'application/json',
                'Content-Length': '2',
                'X-POS-Replay': '1',
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode('utf-8'))

        self.assertEqual(response.status, 504)
        self.assertEqual(payload['code'], 'replay_gateway_idle_timeout')
        self.assertEqual(response.getheader('X-Bosco-Replay-Scope'), 'gateway')

    def test_replay_gateway_enforces_total_timeout(self):
        gateway_port = self._start_gateway(total_timeout=0.2, idle_timeout=1.0)
        connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=5)
        connection.request(
            'POST',
            '/registrar_venta/slow-first-byte',
            body=b'{}',
            headers={
                'Content-Type': 'application/json',
                'Content-Length': '2',
                'X-POS-Replay': '1',
            },
        )
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
