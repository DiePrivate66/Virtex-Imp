from __future__ import annotations

import json
from dataclasses import dataclass

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from pos.models import Empleado, PrintJob, Venta


@dataclass
class CheckResult:
    name: str
    ok: bool
    level: str
    detail: str


class Command(BaseCommand):
    help = 'Verifica estado operativo para WhatsApp + Delivery Quote + Autoimpresion.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--strict',
            action='store_true',
            help='Falla tambien en warnings.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Salida en JSON.',
        )

    def handle(self, *args, **options):
        strict = bool(options.get('strict'))
        as_json = bool(options.get('json'))

        checks: list[CheckResult] = []

        checks.append(self._check_database())
        checks.append(self._check_celery_mode())
        checks.append(self._check_redis())
        checks.append(self._check_whatsapp_env())
        checks.append(self._check_delivery_pool())
        checks.append(self._check_delivery_quotes_backlog())
        checks.append(self._check_print_jobs_backlog())

        if as_json:
            payload = {
                'summary': self._summary(checks),
                'checks': [
                    {
                        'name': c.name,
                        'ok': c.ok,
                        'level': c.level,
                        'detail': c.detail,
                    }
                    for c in checks
                ],
            }
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=True))
        else:
            self._print_human(checks)

        has_error = any((not c.ok and c.level == 'error') for c in checks)
        has_warning = any((not c.ok and c.level == 'warning') for c in checks)
        if has_error or (strict and has_warning):
            raise SystemExit(1)

    def _summary(self, checks: list[CheckResult]) -> dict:
        ok_count = sum(1 for c in checks if c.ok)
        warning_count = sum(1 for c in checks if (not c.ok and c.level == 'warning'))
        error_count = sum(1 for c in checks if (not c.ok and c.level == 'error'))
        return {
            'total': len(checks),
            'ok': ok_count,
            'warnings': warning_count,
            'errors': error_count,
        }

    def _print_human(self, checks: list[CheckResult]):
        summary = self._summary(checks)
        self.stdout.write('== OPS PREFLIGHT ==')
        self.stdout.write(
            f"Checks: total={summary['total']} ok={summary['ok']} "
            f"warnings={summary['warnings']} errors={summary['errors']}"
        )
        for c in checks:
            status = 'OK' if c.ok else ('WARN' if c.level == 'warning' else 'ERROR')
            self.stdout.write(f'[{status}] {c.name}: {c.detail}')

    def _check_database(self) -> CheckResult:
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
            return CheckResult('database', True, 'info', 'conexion activa')
        except Exception as exc:
            return CheckResult('database', False, 'error', f'fallo conexion DB: {exc}')

    def _check_celery_mode(self) -> CheckResult:
        eager = bool(getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False))
        if not settings.DEBUG and eager:
            return CheckResult(
                'celery_mode',
                False,
                'error',
                'CELERY_TASK_ALWAYS_EAGER=True en modo no DEBUG',
            )
        if settings.DEBUG and eager:
            return CheckResult(
                'celery_mode',
                False,
                'warning',
                'modo eager activo (correcto para local, no para produccion)',
            )
        return CheckResult('celery_mode', True, 'info', 'worker/beat esperados')

    def _check_redis(self) -> CheckResult:
        try:
            client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
            pong = client.ping()
            if pong:
                return CheckResult('redis', True, 'info', f'ping ok: {settings.REDIS_URL}')
            return CheckResult('redis', False, 'error', 'sin respuesta de redis')
        except Exception as exc:
            return CheckResult('redis', False, 'error', f'fallo redis: {exc}')

    def _check_whatsapp_env(self) -> CheckResult:
        token = bool(getattr(settings, 'META_WHATSAPP_TOKEN', ''))
        phone_id = bool(getattr(settings, 'META_WHATSAPP_PHONE_NUMBER_ID', ''))
        verify = bool(getattr(settings, 'META_WHATSAPP_VERIFY_TOKEN', ''))
        if token and phone_id and verify:
            return CheckResult('whatsapp_env', True, 'info', 'META credentials configuradas')
        return CheckResult(
            'whatsapp_env',
            False,
            'warning',
            'faltan META_WHATSAPP_TOKEN / META_WHATSAPP_PHONE_NUMBER_ID / META_WHATSAPP_VERIFY_TOKEN',
        )

    def _check_delivery_pool(self) -> CheckResult:
        drivers = Empleado.objects.filter(rol='DELIVERY', activo=True).exclude(telefono='').count()
        if drivers <= 0:
            return CheckResult('delivery_pool', False, 'warning', 'sin drivers DELIVERY activos con telefono')
        return CheckResult('delivery_pool', True, 'info', f'drivers activos={drivers}')

    def _check_delivery_quotes_backlog(self) -> CheckResult:
        pending = Venta.objects.filter(estado='PENDIENTE_COTIZACION').count()
        from django.utils import timezone

        timed_out = Venta.objects.filter(
            estado='PENDIENTE_COTIZACION',
            delivery_quote_deadline_at__isnull=False,
            delivery_quote_deadline_at__lt=timezone.now(),
        ).count()

        if timed_out > 0:
            return CheckResult(
                'delivery_quotes_backlog',
                False,
                'warning',
                f'pendientes={pending}, vencidas={timed_out}',
            )
        return CheckResult('delivery_quotes_backlog', True, 'info', f'pendientes={pending}, vencidas=0')

    def _check_print_jobs_backlog(self) -> CheckResult:
        pending = PrintJob.objects.filter(estado='PENDING').count()
        failed = PrintJob.objects.filter(estado='FAILED').count()
        if failed > 0:
            return CheckResult(
                'print_jobs_backlog',
                False,
                'warning',
                f'pending={pending}, failed={failed}',
            )
        return CheckResult('print_jobs_backlog', True, 'info', f'pending={pending}, failed=0')
