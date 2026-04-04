from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pos.application.cash_register import close_cash_register, get_cash_closing_context, upsert_customer
from pos.application.cash_register.commands import CashRegisterError, verify_pos_pin
from pos.application.cash_movements import register_cash_movement
from pos.application.inventory import get_inventory_panel_context
from pos.application.inventory import register_inventory_movement
from pos.application.printing import build_cash_closing_context
from pos.application.sales import get_pos_home_context
from pos.application.sales.commands import (
    PosSaleError,
    _mark_sale_paid,
    expire_stale_pending_sales,
    purge_expired_idempotency_records,
    reconcile_payment_confirmation,
    resolve_accounting_adjustment,
    resolve_payment_exception,
    register_sale,
)
from pos.infrastructure.notifications.telegram import (
    _reset_telegram_circuit_breaker,
    notify_admin_exception_alert,
)
from pos.infrastructure.tasks.outbox import process_outbox_event, sweep_stale_outbox_events
from pos.models import (
    AccountingAdjustment,
    AuditLog,
    CajaTurno,
    Categoria,
    Cliente,
    Empleado,
    IdempotencyRecord,
    Inventario,
    Location,
    LocationAssignment,
    LocationInventory,
    MovimientoCaja,
    MovimientoInventario,
    Organization,
    OrganizationMembership,
    OutboxEvent,
    PrintJob,
    Producto,
    StaffProfile,
    Venta,
    ensure_system_ledger_account,
)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, SECURE_SSL_REDIRECT=False)
class BoscoV2SalesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='cajero-v2', password='1234', first_name='Juan')
        self.empleado = Empleado.objects.create(
            nombre='Juan Bosco',
            pin='1234',
            rol='CAJERO',
            activo=True,
            usuario=self.user,
        )
        self.categoria = Categoria.objects.create(nombre='Bebidas')
        self.producto = Producto.objects.create(categoria=self.categoria, nombre='Cerveza', precio='3.50')
        Inventario.objects.create(producto=self.producto, stock_actual=10, stock_minimo=2)
        self.turno = CajaTurno.objects.create(usuario=self.user, base_inicial=Decimal('20.00'))
        self.gateway_account = ensure_system_ledger_account(
            organization=self.turno.organization,
            system_code=AccountingAdjustment.SystemLedgerCode.PAYMENT_GATEWAY_CLEARING,
        )
        self.unidentified_receipts_account = ensure_system_ledger_account(
            organization=self.turno.organization,
            system_code=AccountingAdjustment.SystemLedgerCode.UNIDENTIFIED_RECEIPTS,
        )
        self.refund_payable_account = ensure_system_ledger_account(
            organization=self.turno.organization,
            system_code=AccountingAdjustment.SystemLedgerCode.REFUND_PAYABLE,
        )

    def _payload(self, **overrides):
        data = {
            'client_transaction_id': 'sale-v2-001',
            'cart_created_at': '2026-03-31T12:00:00Z',
            'metodo_pago': 'EFECTIVO',
            'tipo_pedido': 'SERVIR',
            'monto_recibido': '10.00',
            'carrito': [
                {'id': self.producto.id, 'cantidad': 2, 'nombre': 'Cerveza', 'nota': ''},
            ],
        }
        data.update(overrides)
        return data

    def test_register_sale_is_idempotent(self):
        first = register_sale(self.user, self._payload())
        second = register_sale(self.user, self._payload())

        self.assertEqual(first.venta.id, second.venta.id)
        self.assertFalse(first.duplicated)
        self.assertTrue(second.duplicated)
        self.assertEqual(Venta.objects.count(), 1)
        self.assertEqual(IdempotencyRecord.objects.filter(status=IdempotencyRecord.Status.COMPLETED).count(), 1)
        self.assertEqual(OutboxEvent.objects.filter(event_type='SALE_PAID_PRINT').count(), 1)
        self.assertEqual(first.venta.payment_status, Venta.PaymentStatus.PAID)

    def test_existing_sale_row_can_backfill_from_legacy_when_canonical_missing(self):
        venta = Venta.objects.create(
            turno=self.turno,
            organization=self.turno.organization,
            location=self.turno.location,
            cliente_nombre='Cliente Legacy',
            total=Decimal('5.00'),
            metodo_pago='EFECTIVO',
            estado='PENDIENTE',
            payment_status=Venta.PaymentStatus.PAID,
        )
        Venta.objects.filter(pk=venta.pk).update(payment_status='', estado_pago='RECHAZADO')
        venta.refresh_from_db()
        venta.cliente_nombre = 'Cliente Legacy Recuperado'
        venta.save()

        self.assertEqual(venta.payment_status, Venta.PaymentStatus.FAILED)
        self.assertEqual(venta.estado_pago, 'RECHAZADO')

    def test_new_sale_rejects_legacy_only_payment_status_input(self):
        with self.assertRaises(ValidationError):
            Venta.objects.create(
                turno=self.turno,
                organization=self.turno.organization,
                location=self.turno.location,
                cliente_nombre='Cliente Legacy',
                total=Decimal('5.00'),
                metodo_pago='EFECTIVO',
                estado='PENDIENTE',
                estado_pago='RECHAZADO',
                payment_status='',
            )

    def test_new_sale_rejects_legacy_only_payment_reference_input(self):
        with self.assertRaises(ValidationError):
            Venta.objects.create(
                turno=self.turno,
                organization=self.turno.organization,
                location=self.turno.location,
                cliente_nombre='Cliente Legacy',
                total=Decimal('5.00'),
                metodo_pago='TARJETA',
                estado='PENDIENTE',
                payment_status=Venta.PaymentStatus.PAID,
                referencia_pago='LEGACY-REF-001',
            )

    def test_payment_status_remains_authoritative_over_legacy_field(self):
        venta = Venta.objects.create(
            turno=self.turno,
            organization=self.turno.organization,
            location=self.turno.location,
            cliente_nombre='Cliente Canonico',
            total=Decimal('7.00'),
            metodo_pago='EFECTIVO',
            estado='PENDIENTE',
            estado_pago='RECHAZADO',
            payment_status=Venta.PaymentStatus.PAID,
            payment_reference='PAGO-REF-001',
            referencia_pago='LEGACY-OLD',
        )

        self.assertEqual(venta.payment_status, Venta.PaymentStatus.PAID)
        self.assertEqual(venta.estado_pago, 'APROBADO')
        self.assertEqual(venta.payment_reference, 'PAGO-REF-001')
        self.assertEqual(venta.referencia_pago, 'PAGO-REF-001')

    def test_payment_status_save_updates_legacy_mirror_without_explicit_estado_pago_field(self):
        venta = Venta.objects.create(
            turno=self.turno,
            organization=self.turno.organization,
            location=self.turno.location,
            cliente_nombre='Cliente Canonico',
            total=Decimal('7.00'),
            metodo_pago='EFECTIVO',
            estado='PENDIENTE',
            payment_status=Venta.PaymentStatus.PENDING,
        )

        venta.payment_status = Venta.PaymentStatus.FAILED
        venta.payment_reference = 'PAGO-REF-002'
        venta.save(update_fields=['payment_status', 'payment_reference'])
        venta.refresh_from_db()

        self.assertEqual(venta.payment_status, Venta.PaymentStatus.FAILED)
        self.assertEqual(venta.estado_pago, 'RECHAZADO')
        self.assertEqual(venta.payment_reference, 'PAGO-REF-002')
        self.assertEqual(venta.referencia_pago, 'PAGO-REF-002')

    def test_register_sale_accepts_canonical_payment_reference_input(self):
        result = register_sale(
            self.user,
            self._payload(
                client_transaction_id='sale-v2-card-canonical-ref',
                metodo_pago='TARJETA',
                payment_reference='PAY-CANONICAL-001',
                tarjeta_tipo='CREDITO',
                tarjeta_marca='VISA',
                monto_recibido='0',
            ),
        )

        self.assertEqual(result.venta.payment_reference, 'PAY-CANONICAL-001')
        self.assertEqual(result.venta.referencia_pago, 'PAY-CANONICAL-001')

    def test_failed_card_payment_restores_inventory_and_marks_sale_failed(self):
        with self.assertRaises(PosSaleError):
            register_sale(
                self.user,
                self._payload(
                    client_transaction_id='sale-v2-card-fail',
                    metodo_pago='TARJETA',
                    referencia_pago='LOTEXYZ',
                    tarjeta_tipo='CREDITO',
                    force_payment_failure=True,
                    monto_recibido='0',
                ),
            )

        venta = Venta.objects.get(client_transaction_id='sale-v2-card-fail')
        legacy_inventory = Inventario.objects.get(producto=self.producto)
        location_inventory = LocationInventory.objects.get(location=venta.location, producto=self.producto)

        self.assertEqual(venta.payment_status, Venta.PaymentStatus.FAILED)
        self.assertEqual(venta.estado, 'CANCELADO')
        self.assertEqual(legacy_inventory.stock_actual, 10)
        self.assertEqual(location_inventory.stock_actual, 10)
        self.assertEqual(OutboxEvent.objects.filter(aggregate_id=str(venta.id)).count(), 0)

    def test_sale_inherits_operating_day_from_turn(self):
        self.turno.operating_day = timezone.localdate()
        self.turno.save(update_fields=['operating_day'])

        result = register_sale(self.user, self._payload(client_transaction_id='sale-v2-day'))

        self.assertEqual(result.venta.operating_day, self.turno.operating_day)

    def test_sale_registration_persists_operator_snapshot_from_application_layer(self):
        result = register_sale(self.user, self._payload(client_transaction_id='sale-v2-snapshot'))

        self.assertIsNotNone(result.venta.operator)
        self.assertEqual(result.venta.operator_display_name_snapshot, result.venta.operator.display_name)

    def test_sale_registration_persists_explicit_detail_pricing_fields(self):
        result = register_sale(self.user, self._payload(client_transaction_id='sale-v2-detail-pricing'))
        detail = result.venta.detalles.get()

        self.assertEqual(detail.precio_bruto_unitario, Decimal('3.50'))
        self.assertEqual(detail.descuento_monto, Decimal('0.00'))
        self.assertEqual(detail.impuesto_monto, Decimal('0.00'))
        self.assertEqual(detail.subtotal_neto, Decimal('7.00'))
        self.assertEqual(detail.pricing_rule_snapshot['source'], 'product.precio')

    def test_pos_home_context_only_exposes_catalog_for_operator_organization(self):
        other_org = Organization.objects.create(slug='org-catalog-other', name='Org Catalog Other')
        other_category = Categoria.objects.create(nombre='Comidas', organization=other_org)
        Producto.objects.create(
            categoria=other_category,
            organization=other_org,
            nombre='Filete',
            precio='9.99',
            activo=True,
        )

        context = get_pos_home_context(self.user)

        self.assertQuerySetEqual(
            context['categorias'].order_by('id'),
            Categoria.objects.filter(organization=self.turno.organization).order_by('id'),
            transform=lambda value: value,
        )
        self.assertQuerySetEqual(
            context['productos'].order_by('id'),
            Producto.objects.filter(organization=self.turno.organization, activo=True).order_by('id'),
            transform=lambda value: value,
        )

    def test_register_sale_rejects_product_from_other_organization(self):
        other_org = Organization.objects.create(slug='org-sale-foreign', name='Org Sale Foreign')
        other_category = Categoria.objects.create(nombre='Especiales', organization=other_org)
        foreign_product = Producto.objects.create(
            categoria=other_category,
            organization=other_org,
            nombre='Langosta',
            precio='20.00',
            activo=True,
        )

        with self.assertRaises(PosSaleError) as exc:
            register_sale(
                self.user,
                self._payload(
                    client_transaction_id='sale-v2-foreign-product',
                    carrito=[{'id': foreign_product.id, 'cantidad': 1, 'nombre': 'Langosta', 'nota': ''}],
                ),
            )

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn('producto no encontrado', exc.exception.message.lower())

    def test_register_sale_rejects_customer_from_other_organization(self):
        other_org = Organization.objects.create(slug='org-customer-foreign', name='Org Customer Foreign')
        foreign_customer = Cliente.objects.create(
            organization=other_org,
            cedula_ruc='0912345678',
            nombre='Cliente Ajeno',
        )

        with self.assertRaises(PosSaleError) as exc:
            register_sale(
                self.user,
                self._payload(
                    client_transaction_id='sale-v2-foreign-customer',
                    cliente_id=foreign_customer.id,
                    cliente_cedula=foreign_customer.cedula_ruc,
                    consumidor_final=False,
                ),
            )

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn('cliente no encontrado', exc.exception.message.lower())

    def test_upsert_customer_allows_same_identity_document_in_different_organizations(self):
        other_org = Organization.objects.create(slug='org-customer-peer', name='Org Customer Peer')

        local_customer = upsert_customer(
            {
                'cedula': '0999999999',
                'nombre': 'Cliente Local',
                'telefono': '0991111111',
            },
            organization=self.turno.organization,
        )
        foreign_customer = upsert_customer(
            {
                'cedula': '0999999999',
                'nombre': 'Cliente Foraneo',
                'telefono': '0992222222',
            },
            organization=other_org,
        )

        self.assertNotEqual(local_customer.id, foreign_customer.id)
        self.assertEqual(local_customer.organization, self.turno.organization)
        self.assertEqual(foreign_customer.organization, other_org)
        self.assertEqual(Cliente.objects.filter(cedula_ruc='0999999999').count(), 2)

    def test_reaper_restores_inventory_for_stale_pending_sales(self):
        with patch('pos.application.sales.commands._process_payment', side_effect=TimeoutError('gateway timeout')):
            with self.assertRaises(TimeoutError):
                register_sale(self.user, self._payload(client_transaction_id='sale-v2-pending-timeout'))

        venta = Venta.objects.get(client_transaction_id='sale-v2-pending-timeout')
        location_inventory = LocationInventory.objects.get(location=venta.location, producto=self.producto)
        legacy_inventory = Inventario.objects.get(producto=self.producto)

        self.assertEqual(venta.payment_status, Venta.PaymentStatus.PENDING)
        self.assertEqual(location_inventory.stock_actual, 8)
        self.assertEqual(legacy_inventory.stock_actual, 8)

        result = expire_stale_pending_sales(stale_before=timezone.now() + timedelta(seconds=1))

        venta.refresh_from_db()
        location_inventory.refresh_from_db()
        legacy_inventory.refresh_from_db()

        self.assertIn(venta.id, result['expired_ids'])
        self.assertEqual(venta.payment_status, Venta.PaymentStatus.VOIDED)
        self.assertEqual(venta.estado, 'CANCELADO')
        self.assertEqual(location_inventory.stock_actual, 10)
        self.assertEqual(legacy_inventory.stock_actual, 10)
        self.assertEqual(
            IdempotencyRecord.objects.get(location=venta.location, client_transaction_id=venta.client_transaction_id).status,
            IdempotencyRecord.Status.FAILED_FINAL,
        )

    def test_inventory_panel_context_is_scoped_to_operator_organization(self):
        other_org = Organization.objects.create(slug='org-inventory-other', name='Org Inventory Other')
        other_category = Categoria.objects.create(nombre='Postres', organization=other_org)
        other_product = Producto.objects.create(
            categoria=other_category,
            organization=other_org,
            nombre='Brownie',
            precio='4.50',
            activo=True,
        )
        Inventario.objects.create(producto=other_product, stock_actual=5, stock_minimo=1)

        context = get_inventory_panel_context(self.user)

        inventory_product_ids = set(context['inventarios'].values_list('producto_id', flat=True))
        self.assertIn(self.producto.id, inventory_product_ids)
        self.assertNotIn(other_product.id, inventory_product_ids)

    def test_register_inventory_movement_sets_scope_explicitly(self):
        register_inventory_movement(
            producto_id=self.producto.id,
            tipo='ENTRADA',
            cantidad_raw=2,
            concepto='Reposicion',
            registrado_por=self.user,
        )

        movement = MovimientoInventario.objects.latest('id')
        self.assertEqual(movement.organization, self.producto.organization)
        self.assertIsNone(movement.location)

    def test_cannot_confirm_sale_after_reaper_voided_it(self):
        with patch('pos.application.sales.commands._process_payment', side_effect=TimeoutError('gateway timeout')):
            with self.assertRaises(TimeoutError):
                register_sale(self.user, self._payload(client_transaction_id='sale-v2-race'))

        venta = Venta.objects.get(client_transaction_id='sale-v2-race')
        expire_stale_pending_sales(stale_before=timezone.now() + timedelta(seconds=1))
        venta.refresh_from_db()
        self.assertEqual(venta.payment_status, Venta.PaymentStatus.VOIDED)

        with self.assertRaises(PosSaleError):
            _mark_sale_paid(
                venta_id=venta.id,
                user=self.user,
                payment_reference='LATEBANK123',
                payment_provider='TEST_GATEWAY',
            )

    def test_db_rejects_cross_tenant_sale_update(self):
        result = register_sale(self.user, self._payload(client_transaction_id='sale-v2-tenant-guard'))
        other_org = Organization.objects.create(slug='org-tenant-guard', name='Org Tenant Guard')

        with self.assertRaises(IntegrityError):
            Venta.objects.filter(id=result.venta.id).update(organization_id=other_org.id)

    def test_purge_expired_idempotency_records_removes_old_completed_rows(self):
        register_sale(self.user, self._payload(client_transaction_id='sale-v2-purge'))
        record = IdempotencyRecord.objects.get(client_transaction_id='sale-v2-purge')
        record.expires_at = timezone.now() - timedelta(days=3)
        record.save(update_fields=['expires_at'])

        result = purge_expired_idempotency_records(purge_before=timezone.now() - timedelta(days=1))

        self.assertIn(record.id, result['purged_ids'])
        self.assertFalse(IdempotencyRecord.objects.filter(id=record.id).exists())

    def test_late_payment_for_voided_sale_creates_manual_review_audit_log(self):
        with patch('pos.application.sales.commands._process_payment', side_effect=TimeoutError('gateway timeout')):
            with self.assertRaises(TimeoutError):
                register_sale(self.user, self._payload(client_transaction_id='sale-v2-orphan'))

        venta = Venta.objects.get(client_transaction_id='sale-v2-orphan')
        expire_stale_pending_sales(stale_before=timezone.now() + timedelta(seconds=1))
        venta.refresh_from_db()
        self.assertEqual(venta.payment_status, Venta.PaymentStatus.VOIDED)

        result = reconcile_payment_confirmation(
            venta_id=venta.id,
            user=self.user,
            payment_reference='BANK-LATE-001',
            payment_provider='TEST_GATEWAY',
            gateway_payload={'status': 'succeeded'},
        )

        venta.refresh_from_db()
        audit = AuditLog.objects.filter(
            event_type='sale.orphan_payment_detected',
            target_id=str(venta.id),
        ).latest('created_at')
        alert_event = OutboxEvent.objects.get(
            aggregate_id=str(venta.id),
            event_type='ADMIN_EXCEPTION_ALERT',
        )

        self.assertEqual(result['status'], 'manual_review_required')
        self.assertEqual(venta.payment_status, Venta.PaymentStatus.VOIDED)
        self.assertTrue(audit.requires_attention)
        self.assertEqual(audit.payload_json['action_source'], 'LATE_PAYMENT_RECONCILIATION')
        self.assertEqual(audit.payload_json['payment_reference'], 'BANK-LATE-001')
        self.assertTrue(audit.payload_json['inventory_snapshot'])
        self.assertEqual(alert_event.status, OutboxEvent.Status.PENDING)
        self.assertEqual(alert_event.priority, OutboxEvent.Priority.CRITICAL)

    def test_resolve_payment_exception_marks_alert_as_resolved(self):
        with patch('pos.application.sales.commands._process_payment', side_effect=TimeoutError('gateway timeout')):
            with self.assertRaises(TimeoutError):
                register_sale(self.user, self._payload(client_transaction_id='sale-v2-resolve-alert'))

        venta = Venta.objects.get(client_transaction_id='sale-v2-resolve-alert')
        expire_stale_pending_sales(stale_before=timezone.now() + timedelta(seconds=1))
        reconcile_payment_confirmation(
            venta_id=venta.id,
            user=self.user,
            payment_reference='BANK-LATE-002',
            payment_provider='TEST_GATEWAY',
            gateway_payload={'status': 'succeeded'},
        )

        alert = AuditLog.objects.filter(
            event_type='sale.orphan_payment_detected',
            target_id=str(venta.id),
        ).latest('created_at')

        resolved = resolve_payment_exception(
            audit_log_id=alert.id,
            user=self.user,
            resolution_note='Revisado y gestionado manualmente',
            resolution_action='REGISTER_INCOME_ONLY',
        )

        alert.refresh_from_db()
        self.assertEqual(resolved.id, alert.id)
        self.assertIsNotNone(alert.resolved_at)
        self.assertEqual(alert.resolved_by, self.user)
        self.assertFalse(alert.requires_attention)
        adjustment = AccountingAdjustment.objects.get(source_audit_log=alert)
        self.assertEqual(adjustment.adjustment_type, AccountingAdjustment.AdjustmentType.ORPHAN_PAYMENT_UNIDENTIFIED)
        self.assertEqual(adjustment.account_bucket, AccountingAdjustment.AccountBucket.PENDING_IDENTIFICATION)
        self.assertEqual(
            adjustment.source_account.system_code,
            AccountingAdjustment.SystemLedgerCode.PAYMENT_GATEWAY_CLEARING,
        )
        self.assertEqual(
            adjustment.destination_account.system_code,
            AccountingAdjustment.SystemLedgerCode.UNIDENTIFIED_RECEIPTS,
        )
        self.assertEqual(adjustment.amount, venta.total)
        self.assertEqual(adjustment.operating_day, venta.operating_day)
        self.assertEqual(adjustment.created_by, self.user)
        self.assertFalse(
            MovimientoCaja.objects.filter(
                turno=venta.turno,
                descripcion__contains=f'Venta #{venta.id}',
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                event_type='sale.orphan_payment_resolved',
                target_id=str(alert.id),
            ).exists()
        )

    def test_reactivate_orphan_payment_requires_current_stock(self):
        with patch('pos.application.sales.commands._process_payment', side_effect=TimeoutError('gateway timeout')):
            with self.assertRaises(TimeoutError):
                register_sale(self.user, self._payload(client_transaction_id='sale-v2-reactivate-no-stock'))

        venta = Venta.objects.get(client_transaction_id='sale-v2-reactivate-no-stock')
        expire_stale_pending_sales(stale_before=timezone.now() + timedelta(seconds=1))
        reconcile_payment_confirmation(
            venta_id=venta.id,
            user=self.user,
            payment_reference='BANK-LATE-003',
            payment_provider='TEST_GATEWAY',
            gateway_payload={'status': 'succeeded'},
        )

        register_sale(
            self.user,
            self._payload(
                client_transaction_id='sale-v2-consume-stock',
                carrito=[{'id': self.producto.id, 'cantidad': 10, 'nombre': 'Cerveza', 'nota': ''}],
                monto_recibido='50.00',
            ),
        )

        alert = AuditLog.objects.filter(
            event_type='sale.orphan_payment_detected',
            target_id=str(venta.id),
        ).latest('created_at')

        with self.assertRaises(PosSaleError):
            resolve_payment_exception(
                audit_log_id=alert.id,
                user=self.user,
                resolution_note='Intento de reactivar sin stock disponible',
                resolution_action='REACTIVATE_SALE',
            )

        venta.refresh_from_db()
        self.assertEqual(venta.payment_status, Venta.PaymentStatus.VOIDED)

    def test_reactivate_orphan_payment_reserves_stock_and_marks_sale_paid(self):
        with patch('pos.application.sales.commands._process_payment', side_effect=TimeoutError('gateway timeout')):
            with self.assertRaises(TimeoutError):
                register_sale(self.user, self._payload(client_transaction_id='sale-v2-reactivate-ok'))

        venta = Venta.objects.get(client_transaction_id='sale-v2-reactivate-ok')
        expire_stale_pending_sales(stale_before=timezone.now() + timedelta(seconds=1))
        reconcile_payment_confirmation(
            venta_id=venta.id,
            user=self.user,
            payment_reference='BANK-LATE-004',
            payment_provider='TEST_GATEWAY',
            gateway_payload={'status': 'succeeded'},
        )

        alert = AuditLog.objects.filter(
            event_type='sale.orphan_payment_detected',
            target_id=str(venta.id),
        ).latest('created_at')

        resolve_payment_exception(
            audit_log_id=alert.id,
            user=self.user,
            resolution_note='Stock validado, se entrega el producto.',
            resolution_action='REACTIVATE_SALE',
        )

        venta.refresh_from_db()
        alert.refresh_from_db()

        self.assertEqual(venta.payment_status, Venta.PaymentStatus.PAID)
        self.assertEqual(venta.estado, 'COCINA')
        self.assertIsNotNone(alert.resolved_at)
        self.assertTrue(
            MovimientoCaja.objects.filter(
                turno=venta.turno,
                concepto='VENTA',
                descripcion__contains=f'Venta #{venta.id}',
            ).exists()
        )

    def test_close_cash_register_is_blocked_by_open_refund_adjustments(self):
        location = Location.get_or_create_default()
        self.turno.organization = location.organization
        self.turno.location = location
        self.turno.operating_day = timezone.localdate()
        self.turno.save(update_fields=['organization', 'location', 'operating_day'])

        alert = AuditLog.objects.create(
            organization=location.organization,
            location=location,
            actor_user=self.user,
            event_type='sale.orphan_payment_detected',
            target_model='Venta',
            target_id='9999',
            payload_json={'payment_reference': 'RF-001'},
            correlation_id='refund-open-test',
        )
        adjustment = AccountingAdjustment.objects.create(
            organization=location.organization,
            location=location,
            source_audit_log=alert,
            adjustment_type=AccountingAdjustment.AdjustmentType.ORPHAN_PAYMENT_REFUND_PENDING,
            account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY,
            source_account=self.gateway_account,
            destination_account=self.refund_payable_account,
            status=AccountingAdjustment.Status.OPEN,
            amount=Decimal('12.50'),
            operating_day=timezone.localdate(),
            payment_reference='RF-001',
            payment_provider='TEST_GATEWAY',
            correlation_id='refund-open-test',
            created_by=self.user,
        )

        closing_context = get_cash_closing_context(self.user)
        self.assertEqual(closing_context['refund_adjustments_open_count'], 1)
        self.assertEqual(closing_context['refund_adjustments_open_total'], Decimal('12.50'))

        with self.assertRaises(CashRegisterError) as exc:
            close_cash_register(self.user, Decimal('20.00'), {'10': 2})

        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn('reembolso', exc.exception.message.lower())
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, AccountingAdjustment.Status.OPEN)

    def test_close_cash_register_can_carry_pending_refund_with_audit_note(self):
        location = Location.get_or_create_default()
        self.turno.organization = location.organization
        self.turno.location = location
        self.turno.operating_day = timezone.localdate()
        self.turno.save(update_fields=['organization', 'location', 'operating_day'])

        alert = AuditLog.objects.create(
            organization=location.organization,
            location=location,
            actor_user=self.user,
            event_type='sale.orphan_payment_detected',
            target_model='Venta',
            target_id='9998',
            payload_json={'payment_reference': 'RF-OVERRIDE-001'},
            correlation_id='refund-override-close-test',
        )
        adjustment = AccountingAdjustment.objects.create(
            organization=location.organization,
            location=location,
            source_audit_log=alert,
            adjustment_type=AccountingAdjustment.AdjustmentType.ORPHAN_PAYMENT_REFUND_PENDING,
            account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY,
            source_account=self.gateway_account,
            destination_account=self.refund_payable_account,
            status=AccountingAdjustment.Status.OPEN,
            amount=Decimal('100.00'),
            operating_day=timezone.localdate(),
            payment_reference='RF-OVERRIDE-001',
            payment_provider='TEST_GATEWAY',
            correlation_id='refund-override-close-test',
            created_by=self.user,
        )

        caja = close_cash_register(
            self.user,
            Decimal('20.00'),
            {'10': 2},
            allow_pending_refund_override=True,
            pending_refund_override_note='No hay liquidez suficiente; deuda trasladada al siguiente turno.',
        )

        adjustment.refresh_from_db()
        self.assertIsNotNone(caja.fecha_cierre)
        self.assertEqual(adjustment.status, AccountingAdjustment.Status.OPEN)
        self.assertTrue(
            AuditLog.objects.filter(
                event_type='cash_register.closed_with_pending_refunds',
                target_id=str(caja.id),
            ).exists()
        )

    def test_close_cash_register_recalculates_totals_without_prior_ui_snapshot(self):
        register_sale(self.user, self._payload(client_transaction_id='sale-v2-close-recalc'))
        MovimientoCaja.objects.create(
            turno=self.turno,
            organization=self.turno.organization,
            location=self.turno.location,
            tipo='INGRESO',
            concepto='PROPINA',
            descripcion='Ingreso extra',
            monto=Decimal('3.00'),
            registrado_por=self.user,
        )
        MovimientoCaja.objects.create(
            turno=self.turno,
            organization=self.turno.organization,
            location=self.turno.location,
            tipo='EGRESO',
            concepto='ALMUERZO',
            descripcion='Consumo interno',
            monto=Decimal('1.25'),
            registrado_por=self.user,
        )

        caja = close_cash_register(self.user, Decimal('30.00'), {'10': 3})
        caja.refresh_from_db()

        self.assertIsNotNone(caja.fecha_cierre)
        self.assertEqual(caja.total_efectivo_sistema, Decimal('7.00'))
        self.assertEqual(caja.total_transferencia_sistema, Decimal('0.00'))
        self.assertEqual(caja.total_otros_sistema, Decimal('0.00'))
        self.assertEqual(caja.diferencia, Decimal('1.25'))

    def test_cash_closing_print_context_uses_canonical_payment_reference(self):
        Venta.objects.create(
            turno=self.turno,
            organization=self.turno.organization,
            location=self.turno.location,
            origen='POS',
            tipo_pedido='LLEVAR',
            estado='PENDIENTE',
            metodo_pago='TARJETA',
            total=Decimal('7.00'),
            payment_status=Venta.PaymentStatus.PAID,
            payment_reference='PAY-CLOSE-001',
            referencia_pago='LEGACY-CLOSE-001',
            tarjeta_tipo='CREDITO',
            tarjeta_marca='VISA',
        )

        closing_context = build_cash_closing_context(self.turno)

        self.assertEqual(len(closing_context['tarjetas_por_referencia']), 1)
        tarjeta = closing_context['tarjetas_por_referencia'][0]
        self.assertEqual(tarjeta['payment_reference'], 'PAY-CLOSE-001')
        self.assertNotIn('referencia_pago', tarjeta)

    def test_register_cash_movement_sets_scope_and_operator_explicitly(self):
        register_cash_movement(
            user=self.user,
            tipo='INGRESO',
            concepto='PROPINA',
            descripcion='Ingreso lateral',
            monto_raw='3.50',
        )

        movement = MovimientoCaja.objects.latest('id')
        self.assertEqual(movement.organization, self.turno.organization)
        self.assertEqual(movement.location, self.turno.location)
        self.assertIsNotNone(movement.operator)

    def test_close_cash_register_rejects_invalid_cash_count_payload(self):
        with self.assertRaises(CashRegisterError) as exc:
            close_cash_register(self.user, Decimal('20.00'), ['10', 2])

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn('conteo', exc.exception.message.lower())

    def test_resolving_refund_adjustment_unblocks_cash_close(self):
        location = Location.get_or_create_default()
        self.turno.organization = location.organization
        self.turno.location = location
        self.turno.operating_day = timezone.localdate()
        self.turno.save(update_fields=['organization', 'location', 'operating_day'])

        alert = AuditLog.objects.create(
            organization=location.organization,
            location=location,
            actor_user=self.user,
            event_type='sale.orphan_payment_detected',
            target_model='Venta',
            target_id='10000',
            payload_json={'payment_reference': 'RF-002'},
            correlation_id='refund-resolve-test',
        )
        adjustment = AccountingAdjustment.objects.create(
            organization=location.organization,
            location=location,
            source_audit_log=alert,
            adjustment_type=AccountingAdjustment.AdjustmentType.ORPHAN_PAYMENT_REFUND_PENDING,
            account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY,
            source_account=self.gateway_account,
            destination_account=self.refund_payable_account,
            status=AccountingAdjustment.Status.OPEN,
            amount=Decimal('8.75'),
            operating_day=timezone.localdate(),
            payment_reference='RF-002',
            payment_provider='TEST_GATEWAY',
            correlation_id='refund-resolve-test',
            created_by=self.user,
        )

        resolved = resolve_accounting_adjustment(
            adjustment_id=adjustment.id,
            user=self.user,
            resolution_note='Reembolso confirmado contra voucher bancario',
            resolution_reference='REFUND-VOUCHER-002',
        )

        adjustment.refresh_from_db()
        self.assertEqual(resolved.id, adjustment.id)
        self.assertEqual(adjustment.status, AccountingAdjustment.Status.RESOLVED)
        self.assertEqual(adjustment.external_reference, 'REFUND-VOUCHER-002')
        inherited_refund_movement = MovimientoCaja.objects.get(
            turno=self.turno,
            tipo='EGRESO',
            concepto=MovimientoCaja.CONCEPTO_REEMBOLSO_HEREDADO,
        )
        self.assertEqual(inherited_refund_movement.monto, Decimal('8.75'))
        self.assertIn(f'ajuste #{adjustment.id}', inherited_refund_movement.descripcion.lower())
        self.assertTrue(
            AuditLog.objects.filter(
                event_type='accounting.adjustment_resolved',
                target_id=str(adjustment.id),
            ).exists()
        )

        closing_context = get_cash_closing_context(self.user)
        self.assertEqual(closing_context['total_reembolsos_heredados_turno'], Decimal('8.75'))

        caja = close_cash_register(self.user, Decimal('20.00'), {'10': 2})
        self.assertIsNotNone(caja.fecha_cierre)

    def test_refund_resolution_from_cash_drawer_requires_available_cash(self):
        location = Location.get_or_create_default()
        self.turno.organization = location.organization
        self.turno.location = location
        self.turno.operating_day = timezone.localdate()
        self.turno.base_inicial = Decimal('2.00')
        self.turno.save(update_fields=['organization', 'location', 'operating_day', 'base_inicial'])

        alert = AuditLog.objects.create(
            organization=location.organization,
            location=location,
            actor_user=self.user,
            event_type='sale.orphan_payment_detected',
            target_model='Venta',
            target_id='10001',
            payload_json={'payment_reference': 'RF-NOCASH-001'},
            correlation_id='refund-no-cash-test',
        )
        adjustment = AccountingAdjustment.objects.create(
            organization=location.organization,
            location=location,
            source_audit_log=alert,
            adjustment_type=AccountingAdjustment.AdjustmentType.ORPHAN_PAYMENT_REFUND_PENDING,
            account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY,
            source_account=self.gateway_account,
            destination_account=self.refund_payable_account,
            status=AccountingAdjustment.Status.OPEN,
            amount=Decimal('50.00'),
            operating_day=timezone.localdate(),
            payment_reference='RF-NOCASH-001',
            payment_provider='TEST_GATEWAY',
            correlation_id='refund-no-cash-test',
            created_by=self.user,
        )

        with self.assertRaises(PosSaleError) as exc:
            resolve_accounting_adjustment(
                adjustment_id=adjustment.id,
                user=self.user,
                resolution_note='Intento de pagar desde caja sin liquidez.',
                resolution_reference='RF-NOCASH-001',
                settlement_mode='CASH_DRAWER',
            )

        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, AccountingAdjustment.Status.OPEN)
        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn('efectivo suficiente', exc.exception.message.lower())
        self.assertEqual(exc.exception.extra_payload['suggested_settlement_mode'], 'EXTERNAL_REFUND')

    def test_refund_resolution_external_mode_does_not_touch_cash_drawer(self):
        location = Location.get_or_create_default()
        self.turno.organization = location.organization
        self.turno.location = location
        self.turno.operating_day = timezone.localdate()
        self.turno.base_inicial = Decimal('2.00')
        self.turno.save(update_fields=['organization', 'location', 'operating_day', 'base_inicial'])

        alert = AuditLog.objects.create(
            organization=location.organization,
            location=location,
            actor_user=self.user,
            event_type='sale.orphan_payment_detected',
            target_model='Venta',
            target_id='10002',
            payload_json={'payment_reference': 'RF-EXT-001'},
            correlation_id='refund-external-test',
        )
        adjustment = AccountingAdjustment.objects.create(
            organization=location.organization,
            location=location,
            source_audit_log=alert,
            adjustment_type=AccountingAdjustment.AdjustmentType.ORPHAN_PAYMENT_REFUND_PENDING,
            account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY,
            source_account=self.gateway_account,
            destination_account=self.refund_payable_account,
            status=AccountingAdjustment.Status.OPEN,
            amount=Decimal('50.00'),
            operating_day=timezone.localdate(),
            payment_reference='RF-EXT-001',
            payment_provider='TEST_GATEWAY',
            correlation_id='refund-external-test',
            created_by=self.user,
        )

        resolve_accounting_adjustment(
            adjustment_id=adjustment.id,
            user=self.user,
            resolution_note='Reembolso ejecutado por transferencia bancaria externa.',
            resolution_reference='BANK-REFUND-001',
            settlement_mode='EXTERNAL_REFUND',
        )

        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, AccountingAdjustment.Status.RESOLVED)
        self.assertFalse(
            MovimientoCaja.objects.filter(
                turno=self.turno,
                tipo='EGRESO',
                concepto=MovimientoCaja.CONCEPTO_REEMBOLSO_HEREDADO,
            ).exists()
        )

    def test_outbox_event_is_not_reprocessed_after_completion(self):
        result = register_sale(self.user, self._payload(client_transaction_id='sale-v2-outbox'))
        event = OutboxEvent.objects.get(aggregate_id=str(result.venta.id), event_type='SALE_PAID_PRINT')

        first_run = process_outbox_event(event.id)
        replay = process_outbox_event(event.id)

        self.assertEqual(first_run['status'], 'done')
        self.assertEqual(replay['status'], 'skipped')
        self.assertEqual(PrintJob.objects.filter(venta=result.venta).count(), 2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PrintJob.objects.create(venta=result.venta, tipo='COMANDA')

    def test_admin_exception_alert_failure_keeps_event_retryable(self):
        event = OutboxEvent.objects.create(
            organization=self.turno.organization,
            location=self.turno.location,
            aggregate_type='Venta',
            aggregate_id='999',
            event_type='ADMIN_EXCEPTION_ALERT',
            priority=OutboxEvent.Priority.CRITICAL,
            payload_json={'alert_type': 'ORPHAN_PAYMENT'},
            correlation_id='alert-failure-test',
        )

        with patch('pos.infrastructure.tasks.outbox.notify_admin_exception_alert', return_value=False):
            with self.assertRaises(RuntimeError):
                process_outbox_event(event.id)

        event.refresh_from_db()
        self.assertEqual(event.status, OutboxEvent.Status.FAILED)
        self.assertGreater(event.available_at, timezone.now() - timedelta(seconds=1))

    @override_settings(
        TELEGRAM_BOT_TOKEN='bot-token',
        TELEGRAM_ADMIN_ALERT_CHAT_ID='chat-id',
        TELEGRAM_CIRCUIT_FAILURE_THRESHOLD=2,
        TELEGRAM_CIRCUIT_OPEN_SECONDS=120,
    )
    def test_telegram_circuit_breaker_short_circuits_after_threshold(self):
        class FakeRedisBreaker:
            def __init__(self):
                self.store = {}

            def delete(self, *keys):
                for key in keys:
                    self.store.pop(key, None)

            def get(self, key):
                return self.store.get(key)

            def incr(self, key):
                value = int(self.store.get(key, 0)) + 1
                self.store[key] = value
                return value

            def expire(self, key, timeout):
                return True

            def setex(self, key, timeout, value):
                self.store[key] = value

        fake_breaker = FakeRedisBreaker()

        with patch('pos.infrastructure.notifications.telegram._get_telegram_breaker_client', return_value=fake_breaker):
            _reset_telegram_circuit_breaker()
            payload = {
                'alert_type': 'ORPHAN_PAYMENT',
                'venta_id': 777,
                'total': '10.00',
            }

            with patch('pos.infrastructure.notifications.telegram.urlrequest.urlopen', side_effect=Exception('telegram down')) as mocked:
                self.assertFalse(notify_admin_exception_alert(payload))
                self.assertFalse(notify_admin_exception_alert(payload))
                self.assertEqual(mocked.call_count, 2)

            with patch('pos.infrastructure.notifications.telegram.urlrequest.urlopen') as mocked:
                self.assertFalse(notify_admin_exception_alert(payload))
                mocked.assert_not_called()

            _reset_telegram_circuit_breaker()

    @override_settings(
        TELEGRAM_BOT_TOKEN='bot-token',
        TELEGRAM_ADMIN_ALERT_CHAT_ID='chat-id',
    )
    def test_telegram_breaker_fails_safe_when_redis_is_unavailable(self):
        payload = {
            'alert_type': 'ORPHAN_PAYMENT',
            'venta_id': 778,
            'total': '12.00',
        }

        with patch(
            'pos.infrastructure.notifications.telegram._get_telegram_breaker_client',
            side_effect=RuntimeError('redis down'),
        ):
            with patch('pos.infrastructure.notifications.telegram.urlrequest.urlopen') as mocked:
                self.assertFalse(notify_admin_exception_alert(payload))
                mocked.assert_not_called()

    def test_outbox_sweeper_prioritizes_critical_events(self):
        low_event = OutboxEvent.objects.create(
            organization=self.turno.organization,
            location=self.turno.location,
            aggregate_type='Venta',
            aggregate_id='1001',
            event_type='SALE_PAID_PRINT',
            priority=OutboxEvent.Priority.HIGH,
            payload_json={'venta_id': 1001, 'print_types': ['TICKET']},
            correlation_id='outbox-high',
        )
        critical_event = OutboxEvent.objects.create(
            organization=self.turno.organization,
            location=self.turno.location,
            aggregate_type='Venta',
            aggregate_id='1002',
            event_type='ADMIN_EXCEPTION_ALERT',
            priority=OutboxEvent.Priority.CRITICAL,
            payload_json={'alert_type': 'ORPHAN_PAYMENT'},
            correlation_id='outbox-critical',
        )
        stale_time = timezone.now() - timedelta(minutes=10)
        OutboxEvent.objects.filter(id__in=[low_event.id, critical_event.id]).update(updated_at=stale_time, available_at=stale_time)

        enqueued_ids = []

        with patch('pos.infrastructure.tasks.outbox.process_outbox_event.delay', side_effect=lambda event_id: enqueued_ids.append(event_id)):
            sweep_stale_outbox_events.run()

        self.assertEqual(enqueued_ids[:2], [critical_event.id, low_event.id])

    def test_outbox_aging_allows_old_normal_event_to_overtake_newer_high_event(self):
        high_event = OutboxEvent.objects.create(
            organization=self.turno.organization,
            location=self.turno.location,
            aggregate_type='Venta',
            aggregate_id='2001',
            event_type='SALE_PAID_PRINT',
            priority=OutboxEvent.Priority.HIGH,
            payload_json={'venta_id': 2001, 'print_types': ['TICKET']},
            correlation_id='outbox-high-fresh',
        )
        normal_event = OutboxEvent.objects.create(
            organization=self.turno.organization,
            location=self.turno.location,
            aggregate_type='Venta',
            aggregate_id='2002',
            event_type='SALE_PAID_PRINT',
            priority=OutboxEvent.Priority.NORMAL,
            payload_json={'venta_id': 2002, 'print_types': ['TICKET']},
            correlation_id='outbox-normal-aged',
        )

        now = timezone.now()
        stale_time = now - timedelta(minutes=10)
        high_created_at = now - timedelta(minutes=6)
        normal_created_at = now - timedelta(minutes=25)

        OutboxEvent.objects.filter(id=high_event.id).update(
            created_at=high_created_at,
            updated_at=stale_time,
            available_at=stale_time,
        )
        OutboxEvent.objects.filter(id=normal_event.id).update(
            created_at=normal_created_at,
            updated_at=stale_time,
            available_at=stale_time,
        )

        enqueued_ids = []

        with patch('pos.infrastructure.tasks.outbox.process_outbox_event.delay', side_effect=lambda event_id: enqueued_ids.append(event_id)):
            sweep_stale_outbox_events.run()

        self.assertEqual(enqueued_ids[:2], [normal_event.id, high_event.id])

    def test_sale_is_rejected_when_cash_turn_crossed_operating_day(self):
        self.turno.operating_day = timezone.localdate() - timedelta(days=1)
        self.turno.fecha_apertura = timezone.now() - timedelta(hours=18)
        self.turno.save(update_fields=['operating_day', 'fecha_apertura'])

        with self.assertRaises(PosSaleError) as exc:
            register_sale(self.user, self._payload(client_transaction_id='sale-v2-stale-turn'))

        self.assertEqual(exc.exception.status_code, 409)


@override_settings(SECURE_SSL_REDIRECT=False)
class BoscoV2StaffTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(slug='org-v2', name='Org V2')
        self.location = Location.objects.create(
            organization=self.organization,
            slug='norte',
            name='Sucursal Norte',
        )
        self.user = User.objects.create_user(username='operador-v2', password='1234', first_name='Juan')
        membership = OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.STAFF,
        )
        self.staff = StaffProfile.objects.create(
            membership=membership,
            operational_role=StaffProfile.OperationalRole.CAJERO,
            pin_hash=make_password('1234'),
            requires_pin_setup=False,
            active=True,
        )
        LocationAssignment.objects.create(
            staff_profile=self.staff,
            location=self.location,
            alias='juan',
            active=True,
        )

    def test_alias_is_case_insensitive_unique_per_location(self):
        other_user = User.objects.create_user(username='otro-v2', password='1234')
        other_membership = OrganizationMembership.objects.create(
            user=other_user,
            organization=self.organization,
            role=OrganizationMembership.Role.STAFF,
        )
        other_staff = StaffProfile.objects.create(
            membership=other_membership,
            operational_role=StaffProfile.OperationalRole.CAJERO,
            pin_hash=make_password('4321'),
            requires_pin_setup=False,
            active=True,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LocationAssignment.objects.create(
                    staff_profile=other_staff,
                    location=self.location,
                    alias='Juan',
                    active=True,
                )

    def test_verify_pos_pin_blocks_after_three_failures(self):
        for _ in range(3):
            with self.assertRaises(CashRegisterError):
                verify_pos_pin('9999', alias='juan', location_uuid=str(self.location.uuid))

        self.staff.refresh_from_db()
        self.assertEqual(self.staff.pin_failed_attempts, 3)
        self.assertIsNotNone(self.staff.pin_blocked_until)

        with self.assertRaises(CashRegisterError) as exc:
            verify_pos_pin('1234', alias='juan', location_uuid=str(self.location.uuid))
        self.assertEqual(exc.exception.status_code, 423)

    def test_customer_api_search_is_scoped_to_staff_organization(self):
        other_org = Organization.objects.create(slug='org-customer-api-other', name='Org Customer API Other')
        Cliente.objects.create(
            organization=other_org,
            cedula_ruc='0922222222',
            nombre='Cliente Ajeno API',
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('api_cliente'), {'cedula': '0922222222'})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['encontrado'])

    def test_customer_api_post_creates_customer_inside_staff_organization(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('api_cliente'),
            data=json.dumps(
                {
                    'cedula': '0933333333',
                    'nombre': 'Cliente POS',
                    'telefono': '0994444444',
                    'direccion': 'Sucursal Norte',
                    'email': 'cliente-pos@example.com',
                }
            ),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        cliente = Cliente.objects.get(id=response.json()['cliente_id'])
        self.assertEqual(cliente.organization, self.organization)
