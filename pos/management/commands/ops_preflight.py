from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Count, Sum
from django.utils import timezone

from pos.ledger_registry import (
    LOCKFILE_PATH,
    MIN_SUPPORTED_QUEUE_SCHEMA,
    REGISTRY_VERSION,
    get_registry_hash,
    get_system_account_defaults_map,
    load_registry_lockfile,
)
from pos.models import (
    AccountingAdjustment,
    AuditLog,
    Empleado,
    IdempotencyRecord,
    LedgerAccount,
    LedgerRegistryActivation,
    OutboxEvent,
    Organization,
    OrganizationLedgerCounterShard,
    OrganizationLedgerState,
    PrintJob,
    Venta,
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    level: str
    detail: str


class Command(BaseCommand):
    help = 'Verifica estado operativo para ledger, pagos resilientes, outbox, WhatsApp y autoimpresion.'

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
        checks.append(self._check_ledger_lockfile())
        checks.append(self._check_ledger_activation())
        checks.append(self._check_ledger_fencing())
        checks.append(self._check_system_ledger_accounts())
        checks.append(self._check_telegram_admin_alerts())
        checks.append(self._check_whatsapp_env())
        checks.append(self._check_pending_sales_backlog())
        checks.append(self._check_idempotency_backlog())
        checks.append(self._check_outbox_backlog())
        checks.append(self._check_payment_exceptions_backlog())
        checks.append(self._check_ledger_shards())
        checks.append(self._check_operational_drift())
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

    def _db_check_failure(self, name: str, exc: Exception) -> CheckResult:
        return CheckResult(name, False, 'error', f'fallo chequeando DB: {exc}')

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

    def _check_ledger_lockfile(self) -> CheckResult:
        try:
            lock = load_registry_lockfile()
        except Exception as exc:
            return CheckResult(
                'ledger_lockfile',
                False,
                'error',
                f'no se pudo leer {LOCKFILE_PATH.name}: {exc}',
            )

        current_hash = get_registry_hash()
        mismatches: list[str] = []
        if lock.get('registry_version') != REGISTRY_VERSION:
            mismatches.append(f"version lock={lock.get('registry_version')} code={REGISTRY_VERSION}")
        if str(lock.get('min_supported_queue_schema')) != str(MIN_SUPPORTED_QUEUE_SCHEMA):
            mismatches.append(
                'queue_schema '
                f"lock={lock.get('min_supported_queue_schema')} code={MIN_SUPPORTED_QUEUE_SCHEMA}"
            )
        if lock.get('registry_hash') != current_hash:
            mismatches.append(f"hash lock={lock.get('registry_hash')} code={current_hash}")

        if mismatches:
            return CheckResult(
                'ledger_lockfile',
                False,
                'error',
                '; '.join(mismatches),
            )

        return CheckResult(
            'ledger_lockfile',
            True,
            'info',
            f'lock ok: version={REGISTRY_VERSION} hash={current_hash[:12]}',
        )

    def _check_ledger_activation(self) -> CheckResult:
        try:
            activation = LedgerRegistryActivation.objects.filter(singleton_key='default').first()
            if not activation:
                return CheckResult(
                    'ledger_activation',
                    False,
                    'error',
                    'falta LedgerRegistryActivation; ejecuta sync_ledger_registry_activation',
                )

            current_hash = get_registry_hash()
            mismatches: list[str] = []
            if activation.active_registry_version != REGISTRY_VERSION:
                mismatches.append(
                    f'version activa={activation.active_registry_version} code={REGISTRY_VERSION}'
                )
            if activation.active_registry_hash != current_hash:
                mismatches.append(
                    f'hash activo={activation.active_registry_hash} code={current_hash}'
                )
            if activation.min_supported_queue_schema != MIN_SUPPORTED_QUEUE_SCHEMA:
                mismatches.append(
                    'queue_schema '
                    f'activo={activation.min_supported_queue_schema} code={MIN_SUPPORTED_QUEUE_SCHEMA}'
                )

            if mismatches and activation.maintenance_mode:
                return CheckResult(
                    'ledger_activation',
                    False,
                    'warning',
                    'maintenance_mode activo; ' + '; '.join(mismatches),
                )
            if mismatches:
                return CheckResult('ledger_activation', False, 'error', '; '.join(mismatches))
            if activation.maintenance_mode:
                return CheckResult(
                    'ledger_activation',
                    False,
                    'warning',
                    'maintenance_mode activo; mutaciones ledger se bloquearan',
                )
            return CheckResult(
                'ledger_activation',
                True,
                'info',
                f'activa version={activation.active_registry_version} hash={activation.active_registry_hash[:12]}',
            )
        except Exception as exc:
            return self._db_check_failure('ledger_activation', exc)

    def _check_ledger_fencing(self) -> CheckResult:
        enabled = bool(getattr(settings, 'LEDGER_VERSION_FENCING_ENABLED', False))
        mutation_paths = tuple(getattr(settings, 'LEDGER_FENCED_MUTATION_PATHS', ()))
        if not enabled:
            return CheckResult(
                'ledger_fencing',
                False,
                'warning' if settings.DEBUG else 'error',
                'LEDGER_VERSION_FENCING_ENABLED=False',
            )
        if not mutation_paths:
            return CheckResult(
                'ledger_fencing',
                False,
                'warning' if settings.DEBUG else 'error',
                'no hay rutas mutantes protegidas en LEDGER_FENCED_MUTATION_PATHS',
            )
        return CheckResult(
            'ledger_fencing',
            True,
            'info',
            f'enabled para {len(mutation_paths)} ruta(s)',
        )

    def _check_system_ledger_accounts(self) -> CheckResult:
        try:
            required_codes = set(get_system_account_defaults_map().keys())
            organization_count = Organization.objects.count()
            if organization_count == 0:
                return CheckResult(
                    'system_ledger_accounts',
                    False,
                    'warning',
                    'sin organizaciones creadas',
                )

            missing_examples: list[str] = []
            missing_orgs = 0
            for organization in Organization.objects.only('id', 'name').iterator():
                present_codes = set(
                    LedgerAccount.objects.filter(
                        organization_id=organization.id,
                        system_code__in=required_codes,
                    ).values_list('system_code', flat=True)
                )
                missing_codes = sorted(required_codes - present_codes)
                if missing_codes:
                    missing_orgs += 1
                    if len(missing_examples) < 3:
                        missing_examples.append(f'{organization.name}: {", ".join(missing_codes)}')

            if missing_orgs:
                return CheckResult(
                    'system_ledger_accounts',
                    False,
                    'warning',
                    f'organizaciones incompletas={missing_orgs}; ejemplos: {" | ".join(missing_examples)}',
                )

            return CheckResult(
                'system_ledger_accounts',
                True,
                'info',
                f'organizaciones verificadas={organization_count}',
            )
        except Exception as exc:
            return self._db_check_failure('system_ledger_accounts', exc)

    def _check_telegram_admin_alerts(self) -> CheckResult:
        bot_token = bool(getattr(settings, 'TELEGRAM_BOT_TOKEN', ''))
        admin_chat = bool(getattr(settings, 'TELEGRAM_ADMIN_ALERT_CHAT_ID', ''))
        if bot_token and admin_chat:
            return CheckResult(
                'telegram_admin_alerts',
                True,
                'info',
                'canal administrativo configurado',
            )
        return CheckResult(
            'telegram_admin_alerts',
            False,
            'warning',
            'faltan TELEGRAM_BOT_TOKEN o TELEGRAM_ADMIN_ALERT_CHAT_ID; alertas criticas no saldran a Telegram',
        )

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

    def _check_pending_sales_backlog(self) -> CheckResult:
        try:
            threshold_seconds = max(60, int(getattr(settings, 'PENDING_PAYMENT_TIMEOUT_SECONDS', 600)))
            cutoff = timezone.now() - timedelta(seconds=threshold_seconds)
            overdue = Venta.objects.filter(
                payment_status=Venta.PaymentStatus.PENDING,
                fecha__lte=cutoff,
            ).count()
            if overdue > 0:
                return CheckResult(
                    'pending_sales_backlog',
                    False,
                    'warning',
                    f'ventas pendientes vencidas={overdue}',
                )
            return CheckResult('pending_sales_backlog', True, 'info', 'sin ventas pendientes vencidas')
        except Exception as exc:
            return self._db_check_failure('pending_sales_backlog', exc)

    def _check_idempotency_backlog(self) -> CheckResult:
        try:
            threshold_seconds = max(60, int(getattr(settings, 'PENDING_PAYMENT_TIMEOUT_SECONDS', 600)))
            cutoff = timezone.now() - timedelta(seconds=threshold_seconds)
            stale_pending = IdempotencyRecord.objects.filter(
                status=IdempotencyRecord.Status.PENDING,
                updated_at__lte=cutoff,
            ).count()
            if stale_pending > 0:
                return CheckResult(
                    'idempotency_backlog',
                    False,
                    'warning',
                    f'registros idempotentes PENDING vencidos={stale_pending}',
                )
            return CheckResult('idempotency_backlog', True, 'info', 'sin registros idempotentes vencidos')
        except Exception as exc:
            return self._db_check_failure('idempotency_backlog', exc)

    def _check_outbox_backlog(self) -> CheckResult:
        try:
            threshold_seconds = max(60, int(getattr(settings, 'OUTBOX_STALE_SECONDS', 300)))
            cutoff = timezone.now() - timedelta(seconds=threshold_seconds)
            pending = OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING).count()
            failed = OutboxEvent.objects.filter(status=OutboxEvent.Status.FAILED).count()
            blocked = OutboxEvent.objects.filter(status=OutboxEvent.Status.BLOCKED).count()
            critical_blocked = OutboxEvent.objects.filter(
                status=OutboxEvent.Status.BLOCKED,
                priority=OutboxEvent.Priority.CRITICAL,
            ).count()
            stale_in_progress = OutboxEvent.objects.filter(
                status=OutboxEvent.Status.IN_PROGRESS,
                updated_at__lte=cutoff,
            ).count()

            detail = (
                f'pending={pending}, failed={failed}, blocked={blocked}, stale_in_progress={stale_in_progress}'
            )
            if critical_blocked > 0:
                return CheckResult('outbox_backlog', False, 'error', detail + f', critical_blocked={critical_blocked}')
            if failed > 0 or blocked > 0 or stale_in_progress > 0:
                return CheckResult('outbox_backlog', False, 'warning', detail)
            return CheckResult('outbox_backlog', True, 'info', detail)
        except Exception as exc:
            return self._db_check_failure('outbox_backlog', exc)

    def _check_payment_exceptions_backlog(self) -> CheckResult:
        try:
            unresolved_alerts = AuditLog.objects.filter(
                event_type='sale.orphan_payment_detected',
                requires_attention=True,
                resolved_at__isnull=True,
            ).count()
            open_refunds = AccountingAdjustment.objects.filter(
                account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY,
                status=AccountingAdjustment.Status.OPEN,
            ).count()
            open_identification = AccountingAdjustment.objects.filter(
                account_bucket=AccountingAdjustment.AccountBucket.PENDING_IDENTIFICATION,
                status=AccountingAdjustment.Status.OPEN,
            ).count()
            if unresolved_alerts > 0 or open_refunds > 0 or open_identification > 0:
                return CheckResult(
                    'payment_exceptions_backlog',
                    False,
                    'warning',
                    (
                        f'alertas_abiertas={unresolved_alerts}, '
                        f'reembolsos_abiertos={open_refunds}, '
                        f'ajustes_por_identificar={open_identification}'
                    ),
                )
            return CheckResult(
                'payment_exceptions_backlog',
                True,
                'info',
                'sin alertas ni ajustes contables abiertos',
            )
        except Exception as exc:
            return self._db_check_failure('payment_exceptions_backlog', exc)

    def _check_ledger_shards(self) -> CheckResult:
        try:
            organization_count = Organization.objects.count()
            if organization_count == 0:
                return CheckResult('ledger_shards', False, 'warning', 'sin organizaciones creadas')

            missing_states = 0
            invalid_states = 0
            counter_drift_orgs = 0
            missing_rows_orgs = 0
            invalid_adjustment_shards = 0
            examples: list[str] = []

            for organization in Organization.objects.only('id', 'slug', 'name').iterator():
                state = OrganizationLedgerState.objects.filter(organization_id=organization.id).only('shard_count').first()
                if not state:
                    missing_states += 1
                    if len(examples) < 3:
                        examples.append(f'{organization.slug}: missing ledger state')
                    continue

                if state.shard_count not in (4, 8, 16, 32):
                    invalid_states += 1
                    if len(examples) < 3:
                        examples.append(f'{organization.slug}: invalid shard_count={state.shard_count}')
                    continue

                shard_rows = list(
                    OrganizationLedgerCounterShard.objects.filter(organization_id=organization.id)
                    .values('shard_id', 'open_adjustment_total', 'open_adjustment_count')
                    .order_by('shard_id')
                )
                shard_ids = {row['shard_id'] for row in shard_rows}
                expected_ids = set(range(state.shard_count))
                if shard_ids != expected_ids:
                    missing_rows_orgs += 1
                    if len(examples) < 3:
                        examples.append(
                            f'{organization.slug}: shard rows {len(shard_rows)}/{state.shard_count}'
                        )

                aggregate = AccountingAdjustment.objects.filter(
                    organization_id=organization.id,
                    status=AccountingAdjustment.Status.OPEN,
                ).aggregate(
                    total=Sum('amount'),
                    count=Count('id'),
                )
                expected_total = Decimal(aggregate.get('total') or '0.00')
                expected_count = int(aggregate.get('count') or 0)
                actual_total = sum((row['open_adjustment_total'] or Decimal('0.00')) for row in shard_rows)
                actual_count = sum(int(row['open_adjustment_count'] or 0) for row in shard_rows)
                invalid_for_org = AccountingAdjustment.objects.filter(
                    organization_id=organization.id,
                    status=AccountingAdjustment.Status.OPEN,
                ).filter(
                    contingency_shard_id__isnull=True,
                ).count()
                invalid_for_org += AccountingAdjustment.objects.filter(
                    organization_id=organization.id,
                    status=AccountingAdjustment.Status.OPEN,
                    contingency_shard_id__gte=state.shard_count,
                ).count()
                invalid_adjustment_shards += invalid_for_org

                if expected_total != actual_total or expected_count != actual_count or invalid_for_org:
                    counter_drift_orgs += 1
                    if len(examples) < 3:
                        examples.append(
                            f'{organization.slug}: expected={expected_count}/{expected_total:.2f} '
                            f'actual={actual_count}/{actual_total:.2f} invalid_adjustments={invalid_for_org}'
                        )

            if missing_states or invalid_states:
                return CheckResult(
                    'ledger_shards',
                    False,
                    'error',
                    (
                        f'missing_states={missing_states}, invalid_states={invalid_states}, '
                        f'missing_row_orgs={missing_rows_orgs}, drift_orgs={counter_drift_orgs}; '
                        f'ejemplos: {" | ".join(examples) if examples else "n/a"}'
                    ),
                )

            if missing_rows_orgs or counter_drift_orgs or invalid_adjustment_shards:
                return CheckResult(
                    'ledger_shards',
                    False,
                    'warning',
                    (
                        f'missing_row_orgs={missing_rows_orgs}, drift_orgs={counter_drift_orgs}, '
                        f'invalid_adjustment_shards={invalid_adjustment_shards}; '
                        f'ejemplos: {" | ".join(examples) if examples else "n/a"}'
                    ),
                )

            return CheckResult(
                'ledger_shards',
                True,
                'info',
                f'organizaciones verificadas={organization_count}',
            )
        except Exception as exc:
            return self._db_check_failure('ledger_shards', exc)

    def _check_operational_drift(self) -> CheckResult:
        try:
            lookback_hours = max(1, int(getattr(settings, 'OPS_PREFLIGHT_OPERATIONAL_DRIFT_LOOKBACK_HOURS', 72)))
            stale_alert_hours = max(1, int(getattr(settings, 'OPS_PREFLIGHT_REPLAY_ALERT_STALE_HOURS', 24)))
            now = timezone.now()
            lookback_cutoff = now - timedelta(hours=lookback_hours)
            stale_cutoff = now - timedelta(hours=stale_alert_hours)

            chronology_estimated_sales = Venta.objects.filter(
                chronology_estimated=True,
                accounting_booked_at__gte=lookback_cutoff,
            ).count()
            unresolved_replay_alerts = AuditLog.objects.filter(
                event_type='sale.post_close_replay_alert',
                requires_attention=True,
                resolved_at__isnull=True,
            ).count()
            stale_unresolved_replay_alerts = AuditLog.objects.filter(
                event_type='sale.post_close_replay_alert',
                requires_attention=True,
                resolved_at__isnull=True,
                created_at__lte=stale_cutoff,
            ).count()

            if stale_unresolved_replay_alerts > 0:
                return CheckResult(
                    'operational_drift',
                    False,
                    'error',
                    (
                        f'chronology_estimated_recent={chronology_estimated_sales}, '
                        f'replay_alerts_open={unresolved_replay_alerts}, '
                        f'replay_alerts_stale={stale_unresolved_replay_alerts}'
                    ),
                )

            if chronology_estimated_sales > 0 or unresolved_replay_alerts > 0:
                return CheckResult(
                    'operational_drift',
                    False,
                    'warning',
                    (
                        f'chronology_estimated_recent={chronology_estimated_sales}, '
                        f'replay_alerts_open={unresolved_replay_alerts}, '
                        f'lookback_hours={lookback_hours}'
                    ),
                )

            return CheckResult(
                'operational_drift',
                True,
                'info',
                f'chronology_estimated_recent=0, replay_alerts_open=0, lookback_hours={lookback_hours}',
            )
        except Exception as exc:
            return self._db_check_failure('operational_drift', exc)

    def _check_delivery_pool(self) -> CheckResult:
        try:
            drivers = Empleado.objects.filter(rol='DELIVERY', activo=True).exclude(telefono='').count()
            if drivers <= 0:
                return CheckResult('delivery_pool', False, 'warning', 'sin drivers DELIVERY activos con telefono')
            return CheckResult('delivery_pool', True, 'info', f'drivers activos={drivers}')
        except Exception as exc:
            return self._db_check_failure('delivery_pool', exc)

    def _check_delivery_quotes_backlog(self) -> CheckResult:
        try:
            pending = Venta.objects.filter(estado='PENDIENTE_COTIZACION').count()

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
        except Exception as exc:
            return self._db_check_failure('delivery_quotes_backlog', exc)

    def _check_print_jobs_backlog(self) -> CheckResult:
        try:
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
        except Exception as exc:
            return self._db_check_failure('print_jobs_backlog', exc)
