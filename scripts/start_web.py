from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pos.infrastructure.replay_gateway import ReplayGatewayConfig, build_gateway_server


TRUE_VALUES = {'1', 'true', 'yes', 'on'}


def _env_bool(name: str, default: bool = False) -> bool:
    return str(os.environ.get(name, 'true' if default else 'false')).strip().lower() in TRUE_VALUES


def _split_env_list(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(item.strip() for item in str(raw).split(',') if item.strip())


def _build_gunicorn_command(*, bind_host: str, bind_port: int) -> list[str]:
    workers = os.environ.get('GUNICORN_WORKERS', '1')
    threads = os.environ.get('GUNICORN_THREADS', '4')
    timeout = os.environ.get('GUNICORN_TIMEOUT', '120')
    return [
        sys.executable,
        '-m',
        'gunicorn.app.wsgiapp',
        'config.wsgi:application',
        '--bind',
        f'{bind_host}:{bind_port}',
        '--workers',
        str(workers),
        '--threads',
        str(threads),
        '--timeout',
        str(timeout),
    ]


def _wait_for_upstream(*, host: str, port: int, timeout_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f'Upstream gunicorn no estuvo listo en {host}:{port} dentro de {timeout_seconds}s.')


def _build_gateway_config(*, external_port: int, upstream_port: int) -> ReplayGatewayConfig:
    retry_after_seconds = int(os.environ.get('POS_REPLAY_RETRY_AFTER_SECONDS', '5'))
    return ReplayGatewayConfig(
        bind_host='0.0.0.0',
        bind_port=external_port,
        upstream_host=os.environ.get('REPLAY_GATEWAY_UPSTREAM_HOST', '127.0.0.1'),
        upstream_port=upstream_port,
        replay_paths=_split_env_list(
            'LEDGER_FENCED_MUTATION_PATHS',
            '/registrar_venta/,/api/reconciliar-pago/',
        ),
        replay_total_timeout_seconds=float(os.environ.get('REPLAY_GATEWAY_TOTAL_TIMEOUT_SECONDS', '10')),
        replay_idle_timeout_seconds=float(os.environ.get('REPLAY_GATEWAY_IDLE_TIMEOUT_SECONDS', '5')),
        upstream_timeout_seconds=float(os.environ.get('REPLAY_GATEWAY_UPSTREAM_TIMEOUT_SECONDS', '120')),
        retry_after_seconds=retry_after_seconds,
        replay_cold_lane_hours=int(
            os.environ.get(
                'REPLAY_GATEWAY_COLD_LANE_HOURS',
                os.environ.get('POS_REPLAY_COLD_LANE_HOURS', '48'),
            )
        ),
        replay_cold_lane_slots=int(
            os.environ.get(
                'REPLAY_GATEWAY_COLD_LANE_SLOTS',
                os.environ.get('POS_REPLAY_COLD_LANE_SLOTS', '2'),
            )
        ),
        replay_cold_slice_seconds=float(os.environ.get('REPLAY_GATEWAY_COLD_SLICE_SECONDS', '120')),
        replay_waiter_ttl_seconds=float(
            os.environ.get(
                'REPLAY_GATEWAY_WAITER_TTL_SECONDS',
                str(max(retry_after_seconds * 3, 30)),
            )
        ),
        replay_bucket_count=int(os.environ.get('REPLAY_GATEWAY_BUCKET_COUNT', '8')),
    )


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    external_port = int(os.environ.get('PORT', '8000'))
    gateway_enabled = _env_bool('REPLAY_GATEWAY_ENABLED', default=False)

    if not gateway_enabled:
        os.execv(
            sys.executable,
            _build_gunicorn_command(bind_host='0.0.0.0', bind_port=external_port),
        )

    upstream_host = os.environ.get('REPLAY_GATEWAY_UPSTREAM_HOST', '127.0.0.1')
    upstream_port = int(os.environ.get('REPLAY_GATEWAY_UPSTREAM_PORT', '18000'))
    upstream_process = subprocess.Popen(
        _build_gunicorn_command(bind_host=upstream_host, bind_port=upstream_port),
        cwd=str(ROOT_DIR),
    )
    try:
        _wait_for_upstream(host=upstream_host, port=upstream_port)
        gateway_server = build_gateway_server(
            _build_gateway_config(external_port=external_port, upstream_port=upstream_port)
        )
    except Exception:
        _terminate_process(upstream_process)
        raise

    stop_event = threading.Event()

    def _shutdown(*_args):
        if stop_event.is_set():
            return
        stop_event.set()
        gateway_server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        gateway_server.serve_forever()
    finally:
        gateway_server.server_close()
        _terminate_process(upstream_process)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
