from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
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
REPLAY_ORGANIZATION_HEADER_NAMES = (
    'X-Bosco-Replay-Organization',
    'X-Organization-Id',
    'X-Location-UUID',
)
REPLAY_ORGANIZATION_BODY_KEYS = (
    'organization_id',
    'organization_slug',
    'location_uuid',
    'queue_session_id',
)


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
    replay_cold_lane_hours: int
    replay_cold_lane_slots: int
    replay_cold_slice_seconds: float
    replay_waiter_ttl_seconds: float
    gateway_header_value: str = 'active'


class ReplayGatewayTimeoutError(TimeoutError):
    def __init__(self, *, reason: str):
        self.reason = reason
        super().__init__(reason)


class ReplayGatewayAdmissionError(Exception):
    def __init__(self, *, lane: str, scope: str, reason: str):
        self.lane = lane
        self.scope = scope
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ReplayRequestContext:
    is_replay: bool = False
    lane: str = 'normal'
    organization_key: str = ''


@dataclass(frozen=True)
class ReplayColdLaneTicket:
    organization_key: str


class ReplayColdLaneCoordinator:
    def __init__(self, *, slots: int, slice_seconds: float, waiter_ttl_seconds: float):
        self._slots = max(1, int(slots))
        self._slice_seconds = max(0.1, float(slice_seconds))
        self._waiter_ttl_seconds = max(0.1, float(waiter_ttl_seconds))
        self._lock = threading.Lock()
        self._active_requests: dict[str, int] = {}
        self._slice_started_at: dict[str, float] = {}
        self._waiting_organizations: dict[str, float] = {}

    def admit(self, *, organization_key: str) -> ReplayColdLaneTicket:
        org_key = organization_key or 'unknown'
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if self._active_requests.get(org_key, 0) > 0:
                raise ReplayGatewayAdmissionError(
                    lane='cold',
                    scope='organization',
                    reason='replay_organization_capacity_exhausted',
                )

            if len(self._active_requests) >= self._slots:
                self._waiting_organizations[org_key] = now
                raise ReplayGatewayAdmissionError(
                    lane='cold',
                    scope='cold_lane',
                    reason='replay_cold_lane_capacity_exhausted',
                )

            slice_started_at = self._slice_started_at.get(org_key)
            if slice_started_at is not None and (now - slice_started_at) >= self._slice_seconds:
                if any(waiting_org != org_key for waiting_org in self._waiting_organizations):
                    self._waiting_organizations[org_key] = now
                    raise ReplayGatewayAdmissionError(
                        lane='cold',
                        scope='cold_lane',
                        reason='replay_cold_lane_draining',
                    )
                self._slice_started_at[org_key] = now
            elif slice_started_at is None:
                self._slice_started_at[org_key] = now

            self._waiting_organizations.pop(org_key, None)
            self._active_requests[org_key] = 1
            return ReplayColdLaneTicket(organization_key=org_key)

    def release(self, ticket: ReplayColdLaneTicket | None) -> None:
        if ticket is None:
            return
        now = time.monotonic()
        with self._lock:
            org_key = ticket.organization_key
            active_count = self._active_requests.get(org_key, 0)
            if active_count <= 1:
                self._active_requests.pop(org_key, None)
            else:
                self._active_requests[org_key] = active_count - 1
            self._prune(now)

    def _prune(self, now: float) -> None:
        expired_waiters = [
            org_key
            for org_key, waiting_since in self._waiting_organizations.items()
            if (now - waiting_since) > self._waiter_ttl_seconds
        ]
        for org_key in expired_waiters:
            self._waiting_organizations.pop(org_key, None)

        retention_seconds = max(self._slice_seconds, self._waiter_ttl_seconds) * 2
        expired_slices = [
            org_key
            for org_key, slice_started_at in self._slice_started_at.items()
            if org_key not in self._active_requests
            and org_key not in self._waiting_organizations
            and (now - slice_started_at) > retention_seconds
        ]
        for org_key in expired_slices:
            self._slice_started_at.pop(org_key, None)


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
        self.cold_lane_coordinator = ReplayColdLaneCoordinator(
            slots=config.replay_cold_lane_slots,
            slice_seconds=config.replay_cold_slice_seconds,
            waiter_ttl_seconds=config.replay_waiter_ttl_seconds,
        )
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
        cold_lane_ticket = None
        replay_context = ReplayRequestContext(is_replay=is_replay)

        try:
            body = self._read_request_body(is_replay=is_replay, deadline=deadline)
            replay_context = self._build_replay_request_context(
                is_replay=is_replay,
                body=body,
            )
            if replay_context.is_replay and replay_context.lane == 'cold':
                cold_lane_ticket = self.server.cold_lane_coordinator.admit(
                    organization_key=replay_context.organization_key,
                )
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
            if replay_context.is_replay:
                self.send_header('X-Bosco-Replay-Gateway', config.gateway_header_value)
                self.send_header('X-Bosco-Replay-Lane', replay_context.lane)
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
                replay_context=replay_context,
                response_started=response_started,
                reason=self._socket_timeout_reason(is_replay=is_replay, deadline=deadline),
                detail=str(exc),
            )
        except ReplayGatewayTimeoutError as exc:
            self._handle_timeout(
                replay_context=replay_context,
                response_started=response_started,
                reason=exc.reason,
                detail=str(exc),
            )
        except ReplayGatewayAdmissionError as exc:
            self._handle_admission_error(exc, replay_context=replay_context)
        except BrokenPipeError:
            self.close_connection = True
        finally:
            if upstream is not None:
                upstream.close()
            self.server.cold_lane_coordinator.release(cold_lane_ticket)

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

    def _build_replay_request_context(self, *, is_replay: bool, body: bytes | None) -> ReplayRequestContext:
        if not is_replay:
            return ReplayRequestContext()

        payload = self._parse_json_body(body)
        lane = self._resolve_replay_lane(payload)
        organization_key = self._resolve_organization_key(payload)
        return ReplayRequestContext(
            is_replay=True,
            lane=lane,
            organization_key=organization_key,
        )

    def _parse_json_body(self, body: bytes | None) -> dict:
        if not body:
            return {}
        content_type = str(self.headers.get('Content-Type') or '')
        if 'json' not in content_type.lower():
            return {}
        try:
            parsed = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _resolve_replay_lane(self, payload: Mapping[str, object]) -> str:
        parsed_client_created_at = _parse_client_created_at_for_gateway(payload.get('client_created_at_raw'))
        if parsed_client_created_at is None:
            return 'normal'
        cold_lane_cutoff = timedelta(hours=max(1, int(self.server.gateway_config.replay_cold_lane_hours)))
        if datetime.now(timezone.utc) - parsed_client_created_at > cold_lane_cutoff:
            return 'cold'
        return 'normal'

    def _resolve_organization_key(self, payload: Mapping[str, object]) -> str:
        for header_name in REPLAY_ORGANIZATION_HEADER_NAMES:
            value = str(self.headers.get(header_name) or '').strip()
            if value:
                return value[:128]
        for body_key in REPLAY_ORGANIZATION_BODY_KEYS:
            value = str(payload.get(body_key) or '').strip()
            if value:
                return value[:128]
        return f'client:{self.client_address[0]}'

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

    def _handle_timeout(self, *, replay_context: ReplayRequestContext, response_started: bool, reason: str, detail: str) -> None:
        if not replay_context.is_replay:
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
            'lane': replay_context.lane,
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
        self.send_header('X-Bosco-Replay-Lane', replay_context.lane)
        self.send_header('X-Bosco-Replay-Scope', 'gateway')
        self.send_header('X-Bosco-Replay-Reason', reason)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _handle_admission_error(self, error: ReplayGatewayAdmissionError, *, replay_context: ReplayRequestContext) -> None:
        payload = {
            'status': 'error',
            'code': 'replay_backpressure',
            'mensaje': 'Sin capacidad inmediata para sincronizacion replay en el gateway.',
            'scope': error.scope,
            'reason': error.reason,
            'lane': error.lane or replay_context.lane,
            'retry_after': self.server.gateway_config.retry_after_seconds,
        }
        body = json.dumps(payload, ensure_ascii=True).encode('utf-8')
        self.send_response(429, 'Too Many Requests')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.send_header('Retry-After', str(self.server.gateway_config.retry_after_seconds))
        self.send_header('X-POS-Replay', '1')
        self.send_header(
            'X-Bosco-Replay-Gateway',
            'draining' if error.reason == 'replay_cold_lane_draining' else 'backpressure',
        )
        self.send_header('X-Bosco-Replay-Lane', error.lane or replay_context.lane)
        self.send_header('X-Bosco-Replay-Scope', error.scope)
        self.send_header('X-Bosco-Replay-Reason', error.reason)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


def _parse_client_created_at_for_gateway(value) -> datetime | None:
    raw_value = str(value or '').strip()
    if not raw_value:
        return None
    normalized_value = raw_value.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
    return parsed.astimezone(timezone.utc)


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
