from __future__ import annotations

from dataclasses import dataclass
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import time
from typing import Mapping
from urllib.parse import urlsplit


REPLAY_TRUE_VALUES = {'1', 'true', 'yes', 'on'}
REPLAY_MUTATION_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
HOP_BY_HOP_HEADERS = {
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailers',
    'transfer-encoding',
    'upgrade',
}


@dataclass(frozen=True)
class ReplayGatewayConfig:
    bind_host: str
    bind_port: int
    upstream_host: str
    upstream_port: int
    replay_paths: tuple[str, ...]
    replay_total_timeout_seconds: float
    replay_idle_timeout_seconds: float
    upstream_timeout_seconds: float
    retry_after_seconds: int
    gateway_header_value: str = 'active'


class ReplayGatewayTimeoutError(TimeoutError):
    def __init__(self, *, reason: str):
        self.reason = reason
        super().__init__(reason)


def is_replay_mutation_request(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    replay_paths: tuple[str, ...],
) -> bool:
    replay_header = str(headers.get('X-POS-Replay') or headers.get('x-pos-replay') or '').strip().lower()
    if replay_header not in REPLAY_TRUE_VALUES:
        return False
    if str(method or '').upper() not in REPLAY_MUTATION_METHODS:
        return False
    request_path = urlsplit(path or '/').path or '/'
    return any(request_path.startswith(prefix) for prefix in replay_paths)


class ReplayGatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, *, config: ReplayGatewayConfig):
        self.gateway_config = config
        super().__init__(server_address, RequestHandlerClass)


class ReplayProxyHandler(BaseHTTPRequestHandler):
    server: ReplayGatewayServer
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def log_message(self, format, *args):
        return

    def _proxy(self):
        config = self.server.gateway_config
        is_replay = is_replay_mutation_request(
            method=self.command,
            path=self.path,
            headers=self.headers,
            replay_paths=config.replay_paths,
        )
        deadline = (
            time.monotonic() + max(0.1, float(config.replay_total_timeout_seconds))
            if is_replay else None
        )
        response_started = False
        upstream = None

        try:
            body = self._read_request_body(is_replay=is_replay, deadline=deadline)
            upstream = http.client.HTTPConnection(
                config.upstream_host,
                config.upstream_port,
                timeout=self._next_timeout(
                    is_replay=is_replay,
                    deadline=deadline,
                    idle_timeout=config.replay_idle_timeout_seconds,
                    default_timeout=config.upstream_timeout_seconds,
                ),
            )
            forwarded_headers = self._build_upstream_headers()
            upstream.request(self.command, self.path, body=body, headers=forwarded_headers)
            upstream.sock.settimeout(
                self._next_timeout(
                    is_replay=is_replay,
                    deadline=deadline,
                    idle_timeout=config.replay_idle_timeout_seconds,
                    default_timeout=config.upstream_timeout_seconds,
                )
            )
            upstream_response = upstream.getresponse()

            self.send_response(upstream_response.status, upstream_response.reason)
            response_started = True
            for header_name, header_value in upstream_response.getheaders():
                if header_name.lower() in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(header_name, header_value)
            if is_replay:
                self.send_header('X-Bosco-Replay-Gateway', config.gateway_header_value)
            self.end_headers()

            while True:
                self._ensure_not_expired(is_replay=is_replay, deadline=deadline, reason='replay_gateway_total_timeout')
                upstream.sock.settimeout(
                    self._next_timeout(
                        is_replay=is_replay,
                        deadline=deadline,
                        idle_timeout=config.replay_idle_timeout_seconds,
                        default_timeout=config.upstream_timeout_seconds,
                    )
                )
                chunk = upstream_response.read(64 * 1024)
                if not chunk:
                    break
                self.connection.settimeout(
                    self._next_timeout(
                        is_replay=is_replay,
                        deadline=deadline,
                        idle_timeout=config.replay_idle_timeout_seconds,
                        default_timeout=config.upstream_timeout_seconds,
                    )
                )
                self.wfile.write(chunk)
                self.wfile.flush()
        except socket.timeout as exc:
            self._handle_timeout(
                is_replay=is_replay,
                response_started=response_started,
                reason=self._socket_timeout_reason(is_replay=is_replay, deadline=deadline),
                detail=str(exc),
            )
        except ReplayGatewayTimeoutError as exc:
            self._handle_timeout(
                is_replay=is_replay,
                response_started=response_started,
                reason=exc.reason,
                detail=str(exc),
            )
        except BrokenPipeError:
            self.close_connection = True
        finally:
            if upstream is not None:
                upstream.close()

    def _build_upstream_headers(self) -> dict[str, str]:
        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            if key.lower() == 'host':
                continue
            headers[key] = value
        headers['Host'] = f'{self.server.gateway_config.upstream_host}:{self.server.gateway_config.upstream_port}'
        headers['X-Forwarded-For'] = self.client_address[0]
        headers['X-Forwarded-Proto'] = 'https' if self.headers.get('X-Forwarded-Proto') == 'https' else 'http'
        return headers

    def _read_request_body(self, *, is_replay: bool, deadline: float | None) -> bytes | None:
        content_length = int(self.headers.get('Content-Length') or 0)
        if content_length <= 0:
            return None
        self.connection.settimeout(
            self._next_timeout(
                is_replay=is_replay,
                deadline=deadline,
                idle_timeout=self.server.gateway_config.replay_idle_timeout_seconds,
                default_timeout=self.server.gateway_config.upstream_timeout_seconds,
            )
        )
        self._ensure_not_expired(is_replay=is_replay, deadline=deadline, reason='replay_gateway_total_timeout')
        return self.rfile.read(content_length)

    def _next_timeout(
        self,
        *,
        is_replay: bool,
        deadline: float | None,
        idle_timeout: float,
        default_timeout: float,
    ) -> float:
        if not is_replay or deadline is None:
            return max(0.1, float(default_timeout))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReplayGatewayTimeoutError(reason='replay_gateway_total_timeout')
        return max(0.1, min(float(idle_timeout), remaining))

    def _ensure_not_expired(self, *, is_replay: bool, deadline: float | None, reason: str) -> None:
        if not is_replay or deadline is None:
            return
        if time.monotonic() >= deadline:
            raise ReplayGatewayTimeoutError(reason=reason)

    def _socket_timeout_reason(self, *, is_replay: bool, deadline: float | None) -> str:
        if not is_replay or deadline is None:
            return 'upstream_timeout'
        if time.monotonic() >= deadline:
            return 'replay_gateway_total_timeout'
        return 'replay_gateway_idle_timeout'

    def _handle_timeout(self, *, is_replay: bool, response_started: bool, reason: str, detail: str) -> None:
        if not is_replay:
            self.close_connection = True
            return
        if response_started:
            self.close_connection = True
            return

        payload = {
            'status': 'error',
            'code': reason,
            'mensaje': 'El gateway de replay corto la solicitud por timeout.',
            'reason': reason,
            'detail': detail,
            'retry_after': self.server.gateway_config.retry_after_seconds,
        }
        body = json.dumps(payload, ensure_ascii=True).encode('utf-8')
        self.send_response(504, 'Gateway Timeout')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.send_header('Retry-After', str(self.server.gateway_config.retry_after_seconds))
        self.send_header('X-POS-Replay', '1')
        self.send_header('X-Bosco-Replay-Gateway', 'timeout')
        self.send_header('X-Bosco-Replay-Scope', 'gateway')
        self.send_header('X-Bosco-Replay-Reason', reason)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


def build_gateway_server(config: ReplayGatewayConfig) -> ReplayGatewayServer:
    return ReplayGatewayServer(
        (config.bind_host, config.bind_port),
        ReplayProxyHandler,
        config=config,
    )


def run_gateway_server(config: ReplayGatewayConfig) -> None:
    server = build_gateway_server(config)
    try:
        server.serve_forever()
    finally:
        server.server_close()
