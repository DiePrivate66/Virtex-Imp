from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from pos.application.sales.commands import PosSaleError, register_sale
from pos.infrastructure.offline import OfflineJournalRuntimeConfig, SegmentedJournalRuntime, recover_segment_prefix
from pos.models import CajaTurno, Categoria, Empleado, Inventario, Producto, Venta


class OfflineJournalCaptureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='offline-cajero', password='1234')
        self.empleado = Empleado.objects.create(
            nombre='Offline Cajero',
            pin='1234',
            rol='CAJERO',
            activo=True,
            usuario=self.user,
        )
        self.turno = CajaTurno.objects.create(usuario=self.user, base_inicial=Decimal('20.00'))
        self.categoria = Categoria.objects.create(
            nombre='Offline Bebidas',
            organization=self.turno.organization,
        )
        self.producto = Producto.objects.create(
            categoria=self.categoria,
            organization=self.turno.organization,
            nombre='Offline Cola',
            precio=Decimal('3.50'),
            activo=True,
        )
        Inventario.objects.create(producto=self.producto, stock_actual=10, stock_minimo=2)

    def _payload(self, **overrides):
        data = {
            'client_transaction_id': 'offline-capture-sale-001',
            'metodo_pago': 'EFECTIVO',
            'tipo_pedido': 'SERVIR',
            'monto_recibido': '10.00',
            'carrito': [
                {'id': self.producto.id, 'cantidad': 2, 'nombre': self.producto.nombre, 'nota': ''},
            ],
        }
        data.update(overrides)
        return data

    def test_register_sale_appends_paid_event_to_offline_journal(self):
        with TemporaryDirectory() as temp_dir:
            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
            ):
                with patch('pos.application.sales.commands.process_outbox_event.delay'):
                    with self.captureOnCommitCallbacks(execute=True):
                        result = register_sale(self.user, self._payload())
                runtime = SegmentedJournalRuntime(
                    config=OfflineJournalRuntimeConfig(root_dir=Path(temp_dir), stream_name='sales')
                )
                limbo = runtime.get_limbo_view()
                self.assertTrue(limbo['segment_path'])
                recovery = recover_segment_prefix(Path(limbo['segment_path']))

        self.assertEqual(result.venta.payment_status, Venta.PaymentStatus.PAID)
        self.assertEqual(limbo['summary']['total_sales'], 1)
        self.assertEqual(limbo['summary']['amount_total'], '7.00')
        self.assertEqual(recovery.record_count, 1)
        self.assertEqual(recovery.records[0]['payload']['capture_event_type'], 'sale.payment_confirmed')

    def test_failed_card_sale_appends_lifecycle_event_without_incrementing_limbo_sales(self):
        with TemporaryDirectory() as temp_dir:
            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
            ):
                with self.captureOnCommitCallbacks(execute=True):
                    with self.assertRaises(PosSaleError):
                        register_sale(
                            self.user,
                            self._payload(
                                client_transaction_id='offline-capture-sale-002',
                                metodo_pago='TARJETA',
                                payment_reference='CARD-FAIL-001',
                                tarjeta_tipo='CREDITO',
                                tarjeta_marca='VISA',
                                monto_recibido='0',
                                force_payment_failure=True,
                            ),
                        )
                runtime = SegmentedJournalRuntime(
                    config=OfflineJournalRuntimeConfig(root_dir=Path(temp_dir), stream_name='sales')
                )
                limbo = runtime.get_limbo_view()
                self.assertTrue(limbo['segment_path'])
                recovery = recover_segment_prefix(Path(limbo['segment_path']))

        self.assertEqual(limbo['summary']['total_sales'], 0)
        self.assertEqual(limbo['summary']['amount_total'], '0.00')
        self.assertEqual(recovery.record_count, 1)
        self.assertEqual(recovery.records[0]['payload']['capture_event_type'], 'sale.payment_failed')
        self.assertEqual(recovery.records[0]['payload']['failure_reason'], 'Pago de tarjeta rechazado por simulacion')
