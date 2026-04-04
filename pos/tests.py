import io
import importlib
import json
import warnings
from decimal import Decimal
from importlib import import_module
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import Group
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.client import RequestFactory
from django.urls import reverse
from django.utils import timezone

from .application.analytics import build_analytics_dashboard_context, build_offline_limbo_context
from .application.printing import build_cash_closing_context
from .application.sales.replay_admission import admit_replay_request
from .application.web_orders import WebOrderError, build_web_orders_payload, create_web_order
from .admin import VentaAdmin
from .application.sales.commands import send_sale_receipt_email
from .infrastructure.delivery import (
    make_delivery_claim_token,
    make_delivery_delivered_token,
    make_delivery_in_transit_token,
)
from .models import (
    AccountingAdjustment,
    AuditLog,
    CajaTurno,
    Categoria,
    Cliente,
    DeliveryQuote,
    Empleado,
    Inventario,
    Location,
    Organization,
    OrganizationLedgerCounterShard,
    PrintJob,
    Producto,
    Venta,
    WhatsAppConversation,
    WhatsAppMessageLog,
    ensure_system_ledger_account,
)
from .presentation.api.web_order_requests import parse_web_order_request
from .tasks import (
    process_customer_confirmation,
    process_delivery_quote_timeout,
    requeue_stuck_print_jobs,
    set_quote_and_notify,
    sweep_delivery_quote_timeouts,
)
from .infrastructure.offline import (
    OfflineJournalRuntimeConfig,
    SegmentJournal,
    SegmentedJournalRuntime,
    recover_segment_prefix,
)
from .application.web_orders.updates import build_web_order_update_request


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    META_SIGNATURE_VALIDATION=False,
    SECURE_SSL_REDIRECT=False,
)
class WhatsAppWebhookTests(TestCase):
    def setUp(self):
        cache.clear()

    def _meta_payload(self, from_number: str, body: str, message_id: str):
        return {
            'entry': [{
                'changes': [{
                    'value': {
                        'messages': [{
                            'from': from_number,
                            'id': message_id,
                            'type': 'text',
                            'text': {'body': body},
                        }]
                    }
                }]
            }]
        }

    def test_webhook_is_idempotent_by_message_sid(self):
        url = reverse('whatsapp_webhook')
        payload = self._meta_payload('593991234567', 'hola', 'wamid.DUP_001')

        r1 = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        r2 = self.client.post(url, data=json.dumps(payload), content_type='application/json')

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(WhatsAppMessageLog.objects.filter(message_sid='wamid.DUP_001').count(), 1)

    def test_webhook_confirms_order_when_conversation_waiting_confirmation(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE',
            metodo_pago='EFECTIVO',
            total='10.00',
            telefono_cliente='+593991112233',
            telefono_cliente_e164='+593991112233',
        )
        WhatsAppConversation.objects.create(
            telefono_e164='+593991112233',
            estado_flujo='ESPERANDO_CONFIRMACION_TOTAL',
            venta=venta,
        )

        url = reverse('whatsapp_webhook')
        payload = self._meta_payload('593991112233', 'SI', 'wamid.CONFIRM_001')
        resp = self.client.post(url, data=json.dumps(payload), content_type='application/json')

        venta.refresh_from_db()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(venta.confirmacion_cliente, 'ACEPTADA')
        self.assertEqual(venta.estado, 'COCINA')
        self.assertEqual(PrintJob.objects.filter(venta=venta, tipo='COMANDA').count(), 1)
        self.assertEqual(PrintJob.objects.filter(venta=venta, tipo='TICKET').count(), 1)

    @override_settings(
        WHATSAPP_INBOUND_RATE_LIMIT_WINDOW_SECONDS=60,
        WHATSAPP_INBOUND_RATE_LIMIT_MAX=2,
    )
    def test_webhook_rate_limit_blocks_excess_messages(self):
        url = reverse('whatsapp_webhook')
        r1 = self.client.post(url, data=json.dumps(self._meta_payload('593991234568', 'hola', 'wamid.RL_1')), content_type='application/json')
        r2 = self.client.post(url, data=json.dumps(self._meta_payload('593991234568', 'hola', 'wamid.RL_2')), content_type='application/json')
        r3 = self.client.post(url, data=json.dumps(self._meta_payload('593991234568', 'hola', 'wamid.RL_3')), content_type='application/json')

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r3.status_code, 200)
        self.assertTrue(
            WhatsAppMessageLog.objects.filter(
                direction='IN',
                telefono_e164='+593991234568',
                status='rate_limited',
            ).exists()
        )


@override_settings(
    DEBUG=True,
    CELERY_TASK_ALWAYS_EAGER=True,
    SECURE_SSL_REDIRECT=False,
)
class DeliveryQuoteRuleTests(TestCase):
    def test_first_quote_wins_and_second_is_discarded(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE_COTIZACION',
            metodo_pago='EFECTIVO',
            total='20.00',
            telefono_cliente='0999999999',
            telefono_cliente_e164='+593999999999',
            delivery_quote_deadline_at=timezone.now() + timedelta(minutes=3),
        )
        d1 = Empleado.objects.create(nombre='Driver 1', pin='1111', rol='DELIVERY', activo=True, telefono='0991111111')
        d2 = Empleado.objects.create(nombre='Driver 2', pin='2222', rol='DELIVERY', activo=True, telefono='0992222222')

        r1 = set_quote_and_notify.delay(venta.id, d1.id, '2.50').get()
        r2 = set_quote_and_notify.delay(venta.id, d2.id, '1.00').get()

        venta.refresh_from_db()
        self.assertEqual(r1.get('status'), 'ok')
        self.assertIn(r2.get('status'), {'late', 'ignored'})
        self.assertEqual(str(venta.costo_envio), '2.50')
        self.assertEqual(venta.estado, 'PENDIENTE')
        self.assertEqual(DeliveryQuote.objects.filter(venta=venta, estado='GANADORA').count(), 1)
        self.assertEqual(DeliveryQuote.objects.filter(venta=venta).count(), 2)

    def test_timeout_keeps_pending_quote_and_logs_customer_notice(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE_COTIZACION',
            metodo_pago='EFECTIVO',
            total='15.00',
            telefono_cliente='0998888888',
            telefono_cliente_e164='+593998888888',
            delivery_quote_deadline_at=timezone.now() - timedelta(seconds=5),
        )

        process_delivery_quote_timeout.delay(venta.id).get()

        venta.refresh_from_db()
        self.assertEqual(venta.estado, 'PENDIENTE_COTIZACION')
        self.assertTrue(
            WhatsAppMessageLog.objects.filter(
                direction='OUT', telefono_e164='+593998888888', status='skipped'
            ).exists()
        )

    def test_sweep_task_dispatches_expired_delivery_quotes(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE_COTIZACION',
            metodo_pago='EFECTIVO',
            total='13.00',
            telefono_cliente='0997777777',
            telefono_cliente_e164='+593997777777',
            delivery_quote_deadline_at=timezone.now() - timedelta(minutes=1),
        )
        res = sweep_delivery_quote_timeouts.delay().get()
        venta.refresh_from_db()

        self.assertEqual(res.get('processed'), 1)
        self.assertEqual(venta.estado, 'PENDIENTE_COTIZACION')


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, SECURE_SSL_REDIRECT=False)
class WebOrderCreationInvariantsTests(TestCase):
    def test_create_web_order_sets_scope_and_operating_day_explicitly(self):
        location = Location.get_or_create_default()
        categoria = Categoria.objects.create(nombre='Hamburguesas', organization=location.organization)
        producto = Producto.objects.create(
            categoria=categoria,
            organization=location.organization,
            nombre='Bosco Burger',
            precio=Decimal('8.50'),
            activo=True,
        )

        venta = create_web_order(
            {
                'nombre': 'Cliente Web',
                'telefono': '0991234567',
                'direccion': 'Av. Central',
                'metodo_pago': 'EFECTIVO',
                'tipo_pedido': 'DOMICILIO',
                'carrito': [{'id': producto.id, 'cantidad': 2, 'nombre': producto.nombre, 'nota': ''}],
            }
        )

        self.assertEqual(venta.location, location)
        self.assertEqual(venta.organization, location.organization)
        self.assertIsNotNone(venta.operating_day)
        self.assertIsNotNone(venta.operated_at_normalized)
        self.assertIsNotNone(venta.accounting_booked_at)
        self.assertFalse(venta.chronology_estimated)
        self.assertEqual(venta.payment_status, Venta.PaymentStatus.PAID)
        self.assertEqual(venta.estado_pago, 'APROBADO')

        detalle = venta.detalles.get()
        self.assertEqual(detalle.precio_bruto_unitario, Decimal('8.50'))
        self.assertEqual(detalle.descuento_monto, Decimal('0.00'))
        self.assertEqual(detalle.impuesto_monto, Decimal('0.00'))
        self.assertEqual(detalle.subtotal_neto, Decimal('17.00'))
        self.assertEqual(detalle.pricing_rule_snapshot['source'], 'product.precio')

    def test_create_web_order_does_not_reuse_customer_from_other_organization(self):
        location = Location.get_or_create_default()
        categoria = Categoria.objects.create(nombre='Pizzas', organization=location.organization)
        producto = Producto.objects.create(
            categoria=categoria,
            organization=location.organization,
            nombre='Bosco Pizza',
            precio=Decimal('11.00'),
            activo=True,
        )
        other_org = Organization.objects.create(slug='org-web-customer-other', name='Org Web Customer Other')
        foreign_customer = Cliente.objects.create(
            organization=other_org,
            cedula_ruc='0912345678',
            nombre='Cliente Ajeno',
        )

        venta = create_web_order(
            {
                'cedula': '0912345678',
                'nombre': 'Cliente Web',
                'telefono': '0991234567',
                'direccion': 'Av. Central',
                'metodo_pago': 'EFECTIVO',
                'tipo_pedido': 'DOMICILIO',
                'carrito': [{'id': producto.id, 'cantidad': 1, 'nombre': producto.nombre, 'nota': ''}],
            }
        )

        self.assertIsNotNone(venta.cliente)
        self.assertNotEqual(venta.cliente_id, foreign_customer.id)
        self.assertEqual(venta.cliente.organization, location.organization)

    def test_create_web_order_appends_paid_event_to_offline_journal(self):
        location = Location.get_or_create_default()
        categoria = Categoria.objects.create(nombre='Combos Web', organization=location.organization)
        producto = Producto.objects.create(
            categoria=categoria,
            organization=location.organization,
            nombre='Combo Bosco',
            precio=Decimal('9.25'),
            activo=True,
        )

        with TemporaryDirectory() as temp_dir:
            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
            ):
                with patch('pos.application.web_orders.commands.send_delivery_quote_requests.delay'):
                    with patch('pos.application.web_orders.commands.process_delivery_quote_timeout.apply_async'):
                        with self.captureOnCommitCallbacks(execute=True):
                            venta = create_web_order(
                                {
                                    'nombre': 'Cliente Web',
                                    'telefono': '0991234567',
                                    'direccion': 'Av. Central',
                                    'metodo_pago': 'EFECTIVO',
                                    'tipo_pedido': 'SERVIR',
                                    'payment_reference': 'WEB-CASH-001',
                                    'carrito': [
                                        {'id': producto.id, 'cantidad': 2, 'nombre': producto.nombre, 'nota': ''},
                                    ],
                                }
                            )

                runtime = SegmentedJournalRuntime(
                    config=OfflineJournalRuntimeConfig(root_dir=Path(temp_dir), stream_name='sales')
                )
                limbo = runtime.get_limbo_view()
                recovery = recover_segment_prefix(Path(limbo['segment_path']))

        self.assertEqual(venta.origen, 'WEB')
        self.assertEqual(limbo['summary']['total_sales'], 1)
        self.assertEqual(limbo['summary']['amount_total'], '18.50')
        self.assertEqual(recovery.record_count, 1)
        self.assertEqual(recovery.records[0]['payload']['capture_event_type'], 'sale.web_order_created')
        self.assertEqual(recovery.records[0]['payload']['journal_capture_source'], 'server_django_web_orders')
        self.assertEqual(recovery.records[0]['payload']['sale_origin'], 'WEB')

    def test_web_orders_payload_exposes_canonical_payment_fields(self):
        location = Location.get_or_create_default()
        venta = Venta.objects.create(
            origen='WEB',
            organization=location.organization,
            location=location,
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE',
            metodo_pago='TARJETA',
            total='14.50',
            payment_status=Venta.PaymentStatus.PAID,
            payment_reference='PAY-WEB-001',
            referencia_pago='LEGACY-IGNORED',
            cliente_nombre='Cliente Web',
            telefono_cliente='0991234567',
            direccion_envio='Av. Central',
        )

        payload = build_web_orders_payload(limit=10)

        self.assertEqual(payload['count'], 1)
        pedido = payload['pedidos'][0]
        self.assertEqual(pedido['id'], venta.id)
        self.assertEqual(pedido['payment_status'], Venta.PaymentStatus.PAID)
        self.assertEqual(pedido['payment_status_display'], venta.get_payment_status_display())
        self.assertEqual(pedido['payment_reference'], 'PAY-WEB-001')
        self.assertNotIn('estado_pago', pedido)
        self.assertNotIn('referencia_pago', pedido)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, SECURE_SSL_REDIRECT=False)
class PrintJobsApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='1234')
        self.client.force_login(self.user)

    def test_retry_failed_print_job(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='LLEVAR',
            estado='COCINA',
            metodo_pago='EFECTIVO',
            total='8.00',
        )
        job = PrintJob.objects.create(venta=venta, tipo='COMANDA', estado='FAILED', reintentos=1, error='Printer down')

        list_resp = self.client.get(reverse('api_print_jobs_failed'))
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(list_resp.json().get('status'), 'ok')
        self.assertEqual(len(list_resp.json().get('jobs', [])), 1)

        retry_resp = self.client.post(reverse('api_print_job_retry', args=[job.id]))
        self.assertEqual(retry_resp.status_code, 200)
        self.assertEqual(retry_resp.json().get('status'), 'ok')

        job.refresh_from_db()
        self.assertEqual(job.estado, 'PENDING')
        self.assertEqual(job.error, '')

    def test_cash_closing_print_context_groups_cards_by_canonical_payment_reference(self):
        location = Location.get_or_create_default()
        caja = CajaTurno.objects.create(
            usuario=self.user,
            base_inicial=Decimal('10.00'),
            organization=location.organization,
            location=location,
        )
        Venta.objects.create(
            turno=caja,
            organization=location.organization,
            location=location,
            origen='POS',
            tipo_pedido='LLEVAR',
            estado='PENDIENTE',
            metodo_pago='TARJETA',
            total='8.00',
            payment_status=Venta.PaymentStatus.PAID,
            payment_reference='PAY-PRINT-001',
            referencia_pago='LEGACY-PRINT-001',
            tarjeta_tipo='CREDITO',
            tarjeta_marca='VISA',
        )

        context = build_cash_closing_context(caja)

        self.assertEqual(len(context['tarjetas_por_referencia']), 1)
        tarjeta = context['tarjetas_por_referencia'][0]
        self.assertEqual(tarjeta['payment_reference'], 'PAY-PRINT-001')
        self.assertNotIn('referencia_pago', tarjeta)

    @override_settings(PRINT_JOB_STUCK_SECONDS=60)
    def test_requeue_stuck_print_jobs_task(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='LLEVAR',
            estado='COCINA',
            metodo_pago='EFECTIVO',
            total='9.00',
        )
        job = PrintJob.objects.create(venta=venta, tipo='COMANDA', estado='IN_PROGRESS')
        PrintJob.objects.filter(id=job.id).update(updated_at=timezone.now() - timedelta(minutes=5))

        result = requeue_stuck_print_jobs.delay().get()
        job.refresh_from_db()

        self.assertGreaterEqual(result.get('requeued', 0), 1)
        self.assertEqual(job.estado, 'PENDING')
        self.assertIn('Reencolado automatico', job.error)


@override_settings(
    DEBUG=True,
    CELERY_TASK_ALWAYS_EAGER=True,
    SECURE_SSL_REDIRECT=False,
)
class CustomerConfirmationTaskTests(TestCase):
    def test_rejected_confirmation_cancels_order(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE',
            metodo_pago='EFECTIVO',
            total='11.00',
            telefono_cliente_e164='+593990000001',
        )

        process_customer_confirmation.delay(venta.id, 'RECHAZADA').get()
        venta.refresh_from_db()

        self.assertEqual(venta.confirmacion_cliente, 'RECHAZADA')
        self.assertEqual(venta.estado, 'CANCELADO')


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, SECURE_SSL_REDIRECT=False)
class IntegrationsHealthApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ops', password='1234')
        self.client.force_login(self.user)

    def test_health_endpoint_returns_expected_sections(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE_COTIZACION',
            metodo_pago='EFECTIVO',
            total='12.00',
            telefono_cliente_e164='+593990000099',
            delivery_quote_deadline_at=timezone.now() - timedelta(minutes=1),
        )
        PrintJob.objects.create(venta=venta, tipo='COMANDA', estado='FAILED', error='paper jam')

        resp = self.client.get(reverse('api_integrations_health'))
        payload = resp.json()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(payload.get('status'), 'ok')
        self.assertIn('whatsapp', payload)
        self.assertIn('delivery_quotes', payload)
        self.assertIn('print_jobs', payload)
        self.assertIn('async', payload)
        self.assertGreaterEqual(payload['delivery_quotes']['timed_out'], 1)
        self.assertGreaterEqual(payload['print_jobs']['failed'], 1)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, SECURE_SSL_REDIRECT=False)
class PosPermissionsTests(TestCase):
    def setUp(self):
        self.admin_group = Group.objects.create(name='Admin')
        self.user = User.objects.create_user(username='sin-grupo', password='1234')
        self.allowed_user = User.objects.create_user(username='con-grupo', password='1234')
        self.allowed_user.groups.add(self.admin_group)

    def test_print_endpoint_requires_allowed_group(self):
        venta = Venta.objects.create(
            origen='POS',
            tipo_pedido='SERVIR',
            estado='COCINA',
            metodo_pago='EFECTIVO',
            total='10.00',
        )

        self.client.force_login(self.user)
        forbidden = self.client.get(reverse('imprimir_ticket', args=[venta.id]))
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.allowed_user)
        allowed = self.client.get(reverse('imprimir_ticket', args=[venta.id]))
        self.assertEqual(allowed.status_code, 200)

    def test_update_web_order_requires_allowed_group(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE',
            metodo_pago='EFECTIVO',
            total='12.00',
        )

        self.client.force_login(self.user)
        forbidden = self.client.post(
            reverse('api_actualizar_pedido'),
            data=json.dumps({'pedido_id': venta.id, 'estado': 'COCINA'}),
            content_type='application/json',
        )
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.allowed_user)
        allowed = self.client.post(
            reverse('api_actualizar_pedido'),
            data=json.dumps({'pedido_id': venta.id, 'estado': 'COCINA'}),
            content_type='application/json',
        )
        self.assertEqual(allowed.status_code, 200)
        venta.refresh_from_db()
        self.assertEqual(venta.estado, 'COCINA')

    def test_update_web_order_rejects_invalid_transition(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE',
            metodo_pago='EFECTIVO',
            total='12.00',
        )

        self.client.force_login(self.allowed_user)
        response = self.client.post(
            reverse('api_actualizar_pedido'),
            data=json.dumps({'pedido_id': venta.id, 'estado': 'EN_CAMINO'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        venta.refresh_from_db()
        self.assertEqual(venta.estado, 'PENDIENTE')


class WebOrderUpdateRequestTests(TestCase):
    def test_build_update_request_accepts_legacy_status(self):
        update_request = build_web_order_update_request({'pedido_id': 7, 'estado': 'COCINA'})

        self.assertEqual(update_request.pedido_id, 7)
        self.assertEqual(update_request.action_name, 'accept_order')
        self.assertFalse(update_request.updates_delivery_cost)

    def test_build_update_request_accepts_delivery_cost_payload(self):
        update_request = build_web_order_update_request({'pedido_id': 9, 'costo_envio': '4.25'})

        self.assertEqual(update_request.pedido_id, 9)
        self.assertEqual(update_request.delivery_cost, '4.25')
        self.assertTrue(update_request.updates_delivery_cost)


@override_settings(SECURE_SSL_REDIRECT=False)
class WebOrderActionApiTests(TestCase):
    def setUp(self):
        self.admin_group = Group.objects.create(name='Admin')
        self.allowed_user = User.objects.create_user(username='acciones-panel', password='1234')
        self.allowed_user.groups.add(self.admin_group)

    def test_update_web_order_allows_delivery_cost_update(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE_COTIZACION',
            metodo_pago='EFECTIVO',
            total='12.00',
        )

        self.client.force_login(self.allowed_user)
        response = self.client.post(
            reverse('api_actualizar_pedido'),
            data=json.dumps({'pedido_id': venta.id, 'costo_envio': '3.50'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        venta.refresh_from_db()
        self.assertEqual(str(venta.costo_envio), '3.50')

    def test_update_web_order_accepts_action_payload(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE',
            metodo_pago='EFECTIVO',
            total='12.00',
        )

        self.client.force_login(self.allowed_user)
        response = self.client.post(
            reverse('api_actualizar_pedido'),
            data=json.dumps({'pedido_id': venta.id, 'accion': 'accept_order'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        venta.refresh_from_db()
        self.assertEqual(venta.estado, 'COCINA')

    def test_update_web_order_rejects_invalid_action(self):
        venta = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE',
            metodo_pago='EFECTIVO',
            total='12.00',
        )

        self.client.force_login(self.allowed_user)
        response = self.client.post(
            reverse('api_actualizar_pedido'),
            data=json.dumps({'pedido_id': venta.id, 'accion': 'teleport'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        venta.refresh_from_db()
        self.assertEqual(venta.estado, 'PENDIENTE')


@override_settings(
    SECURE_SSL_REDIRECT=False,
    LEDGER_VERSION_FENCING_ENABLED=False,
    POS_REPLAY_ADMISSION_ENABLED=True,
    POS_REPLAY_GLOBAL_SLOTS=1,
    POS_REPLAY_ORGANIZATION_SLOTS=1,
    POS_REPLAY_COLD_LANE_SLOTS=1,
    POS_REPLAY_SLOT_TTL_SECONDS=30,
    POS_REPLAY_RETRY_AFTER_SECONDS=7,
    POS_REPLAY_COLD_LANE_HOURS=48,
)
class PosReplayAdmissionApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='replay-cajero', password='1234')
        self.empleado = Empleado.objects.create(
            nombre='Replay Cajero',
            pin='7788',
            rol='CAJERO',
            activo=True,
            usuario=self.user,
        )
        self.user.groups.add(Group.objects.get_or_create(name='Cajero')[0])
        self.turno = CajaTurno.objects.create(usuario=self.user, base_inicial=Decimal('20.00'))
        self.categoria = Categoria.objects.create(nombre='Replay', organization=self.turno.organization)
        self.producto = Producto.objects.create(
            categoria=self.categoria,
            organization=self.turno.organization,
            nombre='Replay Cola',
            precio=Decimal('3.50'),
            activo=True,
        )
        Inventario.objects.create(producto=self.producto, stock_actual=10, stock_minimo=2)
        self.client.force_login(self.user)

    def _payload(self, **overrides):
        data = {
            'client_transaction_id': 'sale-replay-001',
            'metodo_pago': 'EFECTIVO',
            'tipo_pedido': 'SERVIR',
            'monto_recibido': '10.00',
            'carrito': [
                {'id': self.producto.id, 'cantidad': 1, 'nombre': self.producto.nombre, 'nota': ''},
            ],
        }
        data.update(overrides)
        return data

    def test_replay_admission_classifies_old_sale_as_cold_lane(self):
        ticket = admit_replay_request(
            replay_header='1',
            payload={
                'client_created_at_raw': (timezone.now() - timedelta(hours=72)).isoformat(),
            },
            location=self.turno.location,
        )

        self.assertTrue(ticket.is_replay)
        self.assertEqual(ticket.lane, 'cold')
        ticket.release()

    def test_register_sale_returns_429_when_replay_capacity_is_exhausted(self):
        occupied = admit_replay_request(
            replay_header='1',
            payload=self._payload(client_created_at_raw=timezone.now().isoformat()),
            location=self.turno.location,
        )
        try:
            response = self.client.post(
                reverse('registrar_venta'),
                data=json.dumps(self._payload(client_transaction_id='sale-replay-002')),
                content_type='application/json',
                HTTP_X_POS_REPLAY='1',
            )
        finally:
            occupied.release()

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '7')
        self.assertEqual(response['X-Bosco-Replay-Scope'], 'global')
        self.assertEqual(response['X-Bosco-Replay-Reason'], 'replay_global_capacity_exhausted')
        self.assertEqual(response['X-Bosco-Replay-Lane'], 'normal')
        self.assertEqual(response.json()['code'], 'replay_backpressure')

    def test_register_sale_attaches_replay_headers_when_request_is_admitted(self):
        response = self.client.post(
            reverse('registrar_venta'),
            data=json.dumps(self._payload(client_transaction_id='sale-replay-003')),
            content_type='application/json',
            HTTP_X_POS_REPLAY='1',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['X-POS-Replay'], '1')
        self.assertEqual(response['X-Bosco-Replay-Lane'], 'normal')


@override_settings(
    DEBUG=True,
    CELERY_TASK_ALWAYS_EAGER=True,
    REDIS_URL='redis://fake:6379/0',
    SECURE_SSL_REDIRECT=False,
    REPLAY_GATEWAY_ENABLED=True,
    REPLAY_GATEWAY_TOTAL_TIMEOUT_SECONDS=10,
    REPLAY_GATEWAY_IDLE_TIMEOUT_SECONDS=5,
    REPLAY_GATEWAY_UPSTREAM_TIMEOUT_SECONDS=120,
    REPLAY_GATEWAY_UPSTREAM_PORT=18000,
    REPLAY_GATEWAY_COLD_LANE_HOURS=48,
    REPLAY_GATEWAY_COLD_LANE_SLOTS=2,
    REPLAY_GATEWAY_COLD_SLICE_SECONDS=120,
    REPLAY_GATEWAY_WAITER_TTL_SECONDS=30,
)
class OpsPreflightCommandTests(TestCase):
    @patch('pos.management.commands.ops_preflight.Command._check_redis')
    def test_ops_preflight_json_output(self, mock_check_redis):
        from pos.management.commands.ops_preflight import CheckResult

        mock_check_redis.return_value = CheckResult('redis', True, 'info', 'ping ok (mock)')
        out = StringIO()
        call_command('ops_preflight', '--json', stdout=out)
        output = out.getvalue()

        self.assertIn('"summary"', output)
        self.assertIn('"checks"', output)
        self.assertIn('"database"', output)

    @patch('pos.management.commands.ops_preflight.Command._check_redis')
    def test_ops_preflight_json_includes_ledger_and_outbox_checks(self, mock_check_redis):
        from pos.management.commands.ops_preflight import CheckResult

        mock_check_redis.return_value = CheckResult('redis', True, 'info', 'ping ok (mock)')
        out = StringIO()
        call_command('ops_preflight', '--json', stdout=out)
        payload = json.loads(out.getvalue())
        check_names = {item['name'] for item in payload['checks']}

        self.assertIn('ledger_lockfile', check_names)
        self.assertIn('ledger_activation', check_names)
        self.assertIn('system_ledger_accounts', check_names)
        self.assertIn('outbox_backlog', check_names)
        self.assertIn('payment_exceptions_backlog', check_names)
        self.assertIn('replay_gateway', check_names)
        self.assertIn('ledger_shards', check_names)
        self.assertIn('operational_drift', check_names)

    @patch('pos.management.commands.ops_preflight.Command._check_redis')
    def test_ops_preflight_strict_fails_on_warning(self, mock_check_redis):
        from pos.management.commands.ops_preflight import CheckResult

        mock_check_redis.return_value = CheckResult('redis', True, 'info', 'ping ok (mock)')
        with self.assertRaises(SystemExit) as raised:
            call_command('ops_preflight', '--strict', stdout=StringIO())

        self.assertEqual(raised.exception.code, 1)

    def test_ops_preflight_detects_ledger_lockfile_mismatch(self):
        from pos.management.commands.ops_preflight import Command

        command = Command()
        with patch(
            'pos.management.commands.ops_preflight.load_registry_lockfile',
            return_value={
                'registry_version': 'broken-version',
                'registry_hash': 'broken-hash',
                'min_supported_queue_schema': 999,
            },
        ):
            result = command._check_ledger_lockfile()

        self.assertFalse(result.ok)
        self.assertEqual(result.level, 'error')
        self.assertIn('version', result.detail)
        self.assertIn('hash', result.detail)

    def test_ops_preflight_ledger_activation_check_fails_closed_on_db_error(self):
        from pos.management.commands.ops_preflight import Command

        command = Command()
        with patch(
            'pos.management.commands.ops_preflight.LedgerRegistryActivation.objects.filter',
            side_effect=RuntimeError('missing table'),
        ):
            result = command._check_ledger_activation()

        self.assertFalse(result.ok)
        self.assertEqual(result.level, 'error')
        self.assertIn('fallo chequeando DB', result.detail)

    def test_ops_preflight_replay_gateway_detects_invalid_timeout_ordering(self):
        from pos.management.commands.ops_preflight import Command

        command = Command()
        with override_settings(
            REPLAY_GATEWAY_ENABLED=True,
            REPLAY_GATEWAY_TOTAL_TIMEOUT_SECONDS=5,
            REPLAY_GATEWAY_IDLE_TIMEOUT_SECONDS=7,
            REPLAY_GATEWAY_UPSTREAM_TIMEOUT_SECONDS=4,
        ):
            result = command._check_replay_gateway()

        self.assertFalse(result.ok)
        self.assertEqual(result.level, 'error')
        self.assertIn('idle_timeout>total_timeout', result.detail)
        self.assertIn('upstream_timeout<=gateway_timeout', result.detail)

    @patch('pos.management.commands.ops_preflight.Command._load_procfile_web_command')
    def test_ops_preflight_replay_gateway_requires_start_web_wrapper(self, mock_load_procfile_web_command):
        from pos.management.commands.ops_preflight import Command

        mock_load_procfile_web_command.return_value = 'web: gunicorn config.wsgi:application'
        result = Command()._check_replay_gateway()

        self.assertFalse(result.ok)
        self.assertEqual(result.level, 'error')
        self.assertIn('Procfile web no usa scripts/start_web.py', result.detail)

    def test_ops_preflight_offline_journal_requires_root_when_enabled(self):
        from pos.management.commands.ops_preflight import Command

        with override_settings(
            OFFLINE_JOURNAL_ENABLED=True,
            OFFLINE_JOURNAL_ROOT='',
        ):
            result = Command()._check_offline_journal()

        self.assertFalse(result.ok)
        self.assertEqual(result.level, 'error')
        self.assertIn('OFFLINE_JOURNAL_ROOT', result.detail)

    def test_ops_preflight_offline_journal_reports_limbo_summary(self):
        from pos.management.commands.ops_preflight import Command

        with TemporaryDirectory() as temp_dir:
            runtime = SegmentedJournalRuntime(
                config=OfflineJournalRuntimeConfig(
                    root_dir=Path(temp_dir),
                    stream_name='sales',
                )
            )
            runtime.append_sale_event(
                event_id='evt-ops-offline-1',
                payload={'sale_total': '13.25', 'payment_status': 'PAID'},
                client_transaction_id='ops-offline-1',
            )

            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_SEGMENT_MAX_BYTES=104857600,
                OFFLINE_JOURNAL_LIMBO_RECENT_LIMIT=50,
            ):
                result = Command()._check_offline_journal()

        self.assertTrue(result.ok)
        self.assertEqual(result.level, 'info')
        self.assertIn('total_sales=1', result.detail)
        self.assertIn('amount_total=13.25', result.detail)
        self.assertIn('capture_enabled=False', result.detail)

    def test_ops_preflight_offline_journal_reports_recent_pos_and_web_capture_sources(self):
        from pos.management.commands.ops_preflight import Command

        with TemporaryDirectory() as temp_dir:
            runtime = SegmentedJournalRuntime(
                config=OfflineJournalRuntimeConfig(
                    root_dir=Path(temp_dir),
                    stream_name='sales',
                )
            )
            runtime.append_sale_event(
                event_id='evt-ops-capture-pos-1',
                payload={
                    'sale_total': '9.50',
                    'payment_status': 'PAID',
                    'journal_capture_source': 'server_django_sales',
                    'sale_origin': 'POS',
                },
                client_transaction_id='ops-capture-pos-1',
            )
            runtime.append_sale_event(
                event_id='evt-ops-capture-web-1',
                payload={
                    'sale_total': '4.00',
                    'payment_status': 'PAID',
                    'journal_capture_source': 'server_django_web_orders',
                    'sale_origin': 'WEB',
                },
                client_transaction_id='ops-capture-web-1',
            )

            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
                OPS_PREFLIGHT_OFFLINE_CAPTURE_LOOKBACK_HOURS=24,
            ):
                result = Command()._check_offline_journal()

        self.assertTrue(result.ok)
        self.assertEqual(result.level, 'info')
        self.assertIn('capture_enabled=True', result.detail)
        self.assertIn('recent_capture_records=2', result.detail)
        self.assertIn('recent_origins=POS,WEB', result.detail)

    def test_ops_preflight_offline_journal_warns_when_shadow_capture_misses_origin(self):
        from pos.management.commands.ops_preflight import Command

        with TemporaryDirectory() as temp_dir:
            runtime = SegmentedJournalRuntime(
                config=OfflineJournalRuntimeConfig(
                    root_dir=Path(temp_dir),
                    stream_name='sales',
                )
            )
            runtime.append_sale_event(
                event_id='evt-ops-capture-pos-only',
                payload={
                    'sale_total': '8.00',
                    'payment_status': 'PAID',
                    'journal_capture_source': 'server_django_sales',
                    'sale_origin': 'POS',
                },
                client_transaction_id='ops-capture-pos-only',
            )

            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
                OPS_PREFLIGHT_OFFLINE_CAPTURE_LOOKBACK_HOURS=24,
            ):
                result = Command()._check_offline_journal()

        self.assertFalse(result.ok)
        self.assertEqual(result.level, 'warning')
        self.assertIn('capture_enabled=True', result.detail)
        self.assertIn('recent_origins=POS', result.detail)
        self.assertIn('missing_recent_origins=WEB', result.detail)

    def test_ops_preflight_ledger_shards_detects_counter_drift(self):
        from pos.management.commands.ops_preflight import Command

        location = Location.get_or_create_default()
        user = User.objects.create_user(username='ops-shards', password='1234')
        source_account = ensure_system_ledger_account(
            organization=location.organization,
            system_code=AccountingAdjustment.SystemLedgerCode.PAYMENT_GATEWAY_CLEARING,
        )
        destination_account = ensure_system_ledger_account(
            organization=location.organization,
            system_code=AccountingAdjustment.SystemLedgerCode.UNIDENTIFIED_RECEIPTS,
        )
        alert = AuditLog.objects.create(
            organization=location.organization,
            location=location,
            actor_user=user,
            event_type='sale.orphan_payment_detected',
            target_model='Venta',
            target_id='ops-ledger-shards',
            payload_json={'payment_reference': 'OPS-SHARD-001'},
            correlation_id='ops-ledger-shards',
        )
        adjustment = AccountingAdjustment.objects.create(
            organization=location.organization,
            location=location,
            source_audit_log=alert,
            adjustment_type=AccountingAdjustment.AdjustmentType.ORPHAN_PAYMENT_UNIDENTIFIED,
            account_bucket=AccountingAdjustment.AccountBucket.PENDING_IDENTIFICATION,
            source_account=source_account,
            destination_account=destination_account,
            status=AccountingAdjustment.Status.OPEN,
            amount=Decimal('12.50'),
            operating_day=timezone.localdate(),
            payment_reference='OPS-SHARD-001',
            payment_provider='TEST',
            correlation_id='ops-ledger-shards',
            created_by=user,
        )
        OrganizationLedgerCounterShard.objects.filter(
            organization=location.organization,
            shard_id=adjustment.contingency_shard_id,
        ).update(
            open_adjustment_total=Decimal('0.00'),
            open_adjustment_count=0,
        )

        result = Command()._check_ledger_shards()

        self.assertFalse(result.ok)
        self.assertEqual(result.level, 'warning')
        self.assertIn('drift_orgs=1', result.detail)

    @override_settings(
        OPS_PREFLIGHT_OPERATIONAL_DRIFT_LOOKBACK_HOURS=72,
        OPS_PREFLIGHT_REPLAY_ALERT_STALE_HOURS=1,
    )
    def test_ops_preflight_operational_drift_detects_stale_replay_alerts(self):
        from pos.management.commands.ops_preflight import Command

        location = Location.get_or_create_default()
        user = User.objects.create_user(username='ops-drift', password='1234')
        venta = Venta.objects.create(
            origen='POS',
            tipo_pedido='SERVIR',
            estado='PENDIENTE',
            metodo_pago='EFECTIVO',
            payment_status=Venta.PaymentStatus.PAID,
            total=Decimal('8.00'),
            organization=location.organization,
            location=location,
            chronology_estimated=True,
            queue_session_id='ops-drift-a',
            session_seq_no=1,
            accounting_booked_at=timezone.now(),
        )
        alert = AuditLog.objects.create(
            organization=location.organization,
            location=location,
            actor_user=user,
            event_type='sale.post_close_replay_alert',
            target_model='Venta',
            target_id=str(venta.id),
            requires_attention=True,
            payload_json={'queue_session_id': 'ops-drift-a'},
        )
        AuditLog.objects.filter(pk=alert.pk).update(created_at=timezone.now() - timedelta(hours=2))

        result = Command()._check_operational_drift()

        self.assertFalse(result.ok)
        self.assertEqual(result.level, 'error')
        self.assertIn('chronology_estimated_recent=1', result.detail)
        self.assertIn('replay_alerts_stale=1', result.detail)


@override_settings(SECURE_SSL_REDIRECT=False)
class AnalyticsReplayTimelineTests(TestCase):
    def setUp(self):
        self.location = Location.get_or_create_default()
        self.user = User.objects.create_superuser(
            username='analytics-admin',
            password='1234',
            email='analytics@example.com',
        )

    def test_dashboard_context_includes_replay_timeline_alerts_and_estimated_sales_count(self):
        venta = Venta.objects.create(
            origen='POS',
            tipo_pedido='SERVIR',
            estado='PENDIENTE',
            metodo_pago='EFECTIVO',
            payment_status=Venta.PaymentStatus.PAID,
            total=Decimal('9.50'),
            organization=self.location.organization,
            location=self.location,
            chronology_estimated=True,
            queue_session_id='offline-dashboard-a',
            session_seq_no=3,
            operated_at_normalized=timezone.now() - timedelta(days=1),
            accounting_booked_at=timezone.now(),
        )
        alert = AuditLog.objects.create(
            organization=self.location.organization,
            location=self.location,
            actor_user=self.user,
            event_type='sale.post_close_replay_alert',
            target_model='Venta',
            target_id=str(venta.id),
            requires_attention=True,
            payload_json={
                'queue_session_id': 'offline-dashboard-a',
                'session_seq_no': 3,
                'operated_operating_day': '2026-04-01',
                'accounting_operating_day': '2026-04-02',
                'chronology_estimated': True,
            },
        )

        context = build_analytics_dashboard_context(periodo='semana')

        self.assertEqual(context['chronology_estimated_sales_count'], 1)
        self.assertEqual(context['replay_timeline_alerts_open_count'], 1)
        self.assertEqual(context['replay_timeline_alerts_open'][0].id, alert.id)

    def test_resolver_alerta_replay_marks_alert_resolved(self):
        venta = Venta.objects.create(
            origen='POS',
            tipo_pedido='SERVIR',
            estado='PENDIENTE',
            metodo_pago='EFECTIVO',
            payment_status=Venta.PaymentStatus.PAID,
            total=Decimal('11.00'),
            organization=self.location.organization,
            location=self.location,
            chronology_estimated=True,
            queue_session_id='offline-dashboard-b',
            session_seq_no=8,
            operated_at_normalized=timezone.now() - timedelta(days=2),
            accounting_booked_at=timezone.now(),
        )
        alert = AuditLog.objects.create(
            organization=self.location.organization,
            location=self.location,
            actor_user=self.user,
            event_type='sale.post_close_replay_alert',
            target_model='Venta',
            target_id=str(venta.id),
            requires_attention=True,
            payload_json={
                'queue_session_id': 'offline-dashboard-b',
                'session_seq_no': 8,
                'operated_operating_day': '2026-04-01',
                'accounting_operating_day': '2026-04-03',
                'chronology_estimated': True,
            },
            correlation_id='replay-resolution-test',
        )

        self.client.force_login(self.user)
        response = self.client.post(
            reverse('resolver_alerta_replay'),
            data={'audit_log_id': alert.id, 'resolution_note': 'Cierre revisado manualmente por administracion.'},
        )

        self.assertEqual(response.status_code, 302)
        alert.refresh_from_db()
        self.assertFalse(alert.requires_attention)
        self.assertIsNotNone(alert.resolved_at)
        self.assertEqual(alert.resolved_by, self.user)
        self.assertTrue(
            AuditLog.objects.filter(
                event_type='sale.post_close_replay_alert_resolved',
                target_id=str(alert.id),
                correlation_id='replay-resolution-test',
            ).exists()
        )


@override_settings(SECURE_SSL_REDIRECT=False)
class OfflineLimboDashboardTests(TestCase):
    def setUp(self):
        self.location = Location.get_or_create_default()
        self.admin_user = User.objects.create_superuser(
            username='offline-limbo-admin',
            password='1234',
            email='offline-limbo@example.com',
        )
        self.user = User.objects.create_user(
            username='offline-limbo-user',
            password='1234',
        )

    def test_build_offline_limbo_context_reports_disabled_runtime(self):
        with override_settings(
            OFFLINE_JOURNAL_ENABLED=False,
            OFFLINE_JOURNAL_ROOT='',
        ):
            context = build_offline_limbo_context()

        self.assertEqual(context['status'], 'disabled')
        self.assertIn('desactivado', context['detail'].lower())

    def test_dashboard_offline_limbo_renders_summary_and_recent_events(self):
        with TemporaryDirectory() as temp_dir:
            runtime = SegmentedJournalRuntime(
                config=OfflineJournalRuntimeConfig(
                    root_dir=Path(temp_dir),
                    stream_name='sales',
                )
            )
            runtime.append_sale_event(
                event_id='evt-offline-view-1',
                payload={
                    'sale_total': '7.25',
                    'payment_status': 'PAID',
                    'payment_reference': 'OFFLINE-VIEW-001',
                    'journal_capture_source': 'server_django_sales',
                    'capture_event_type': 'sale.payment_confirmed',
                    'sale_origin': 'POS',
                    'display_name': 'Cliente Offline',
                },
                client_transaction_id='offline-view-001',
                queue_session_id='offline-view-session',
                session_seq_no=4,
            )

            self.client.force_login(self.admin_user)
            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
            ):
                response = self.client.get(reverse('dashboard_offline_limbo'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['status'], 'ready')
        self.assertEqual(response.context['limbo']['summary']['total_sales'], 1)
        self.assertEqual(response.context['limbo']['summary']['amount_total'], '7.25')
        self.assertEqual(len(response.context['recent_events']), 1)
        self.assertEqual(response.context['recent_events'][0]['payment_reference'], 'OFFLINE-VIEW-001')
        self.assertContains(response, 'Limbo Offline')
        self.assertContains(response, 'OFFLINE-VIEW-001')

    def test_dashboard_offline_limbo_renders_sealed_segment_history(self):
        with TemporaryDirectory() as temp_dir:
            runtime = SegmentedJournalRuntime(
                config=OfflineJournalRuntimeConfig(
                    root_dir=Path(temp_dir),
                    stream_name='sales',
                )
            )
            runtime.append_sale_event(
                event_id='evt-offline-sealed-1',
                payload={
                    'sale_total': '4.75',
                    'payment_status': 'PAID',
                    'payment_reference': 'OFFLINE-SEALED-001',
                    'journal_capture_source': 'server_django_sales',
                    'capture_event_type': 'sale.payment_confirmed',
                    'sale_origin': 'POS',
                },
                client_transaction_id='offline-sealed-001',
            )
            sealed_snapshot = runtime.seal_active_segment()
            runtime.append_sale_event(
                event_id='evt-offline-active-1',
                payload={
                    'sale_total': '8.35',
                    'payment_status': 'PAID',
                    'payment_reference': 'OFFLINE-ACTIVE-001',
                    'journal_capture_source': 'server_django_sales',
                    'capture_event_type': 'sale.payment_confirmed',
                    'sale_origin': 'POS',
                },
                client_transaction_id='offline-active-001',
            )

            self.client.force_login(self.admin_user)
            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
                OFFLINE_JOURNAL_HISTORY_LIMIT=5,
            ):
                response = self.client.get(reverse('dashboard_offline_limbo'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Historial De Segmentos Sellados')
        self.assertContains(response, sealed_snapshot['segment_id'])
        self.assertEqual(len(response.context['sealed_segments']), 1)
        self.assertEqual(response.context['sealed_segments'][0]['status'], 'sealed')

    def test_dashboard_offline_limbo_redirects_non_admin_user(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('dashboard_offline_limbo'))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('pos_index'))

    def test_dashboard_offline_limbo_json_returns_payload_for_admin(self):
        with TemporaryDirectory() as temp_dir:
            runtime = SegmentedJournalRuntime(
                config=OfflineJournalRuntimeConfig(
                    root_dir=Path(temp_dir),
                    stream_name='sales',
                )
            )
            runtime.append_sale_event(
                event_id='evt-offline-json-1',
                payload={
                    'sale_total': '5.50',
                    'payment_status': 'PAID',
                    'payment_reference': 'OFFLINE-JSON-001',
                    'journal_capture_source': 'server_django_sales',
                    'capture_event_type': 'sale.payment_confirmed',
                    'sale_origin': 'POS',
                },
                client_transaction_id='offline-json-001',
            )

            self.client.force_login(self.admin_user)
            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
            ):
                response = self.client.get(reverse('dashboard_offline_limbo_json'))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'ready')
        self.assertEqual(payload['limbo']['summary']['total_sales'], 1)
        self.assertEqual(payload['recent_events'][0]['payment_reference'], 'OFFLINE-JSON-001')
        self.assertIn('refreshed_at', payload)

    def test_dashboard_offline_limbo_json_includes_sealed_segment_history(self):
        with TemporaryDirectory() as temp_dir:
            runtime = SegmentedJournalRuntime(
                config=OfflineJournalRuntimeConfig(
                    root_dir=Path(temp_dir),
                    stream_name='sales',
                )
            )
            runtime.append_sale_event(
                event_id='evt-offline-sealed-json-1',
                payload={
                    'sale_total': '3.15',
                    'payment_status': 'PAID',
                    'payment_reference': 'OFFLINE-HISTORY-001',
                    'journal_capture_source': 'server_django_sales',
                    'capture_event_type': 'sale.payment_confirmed',
                    'sale_origin': 'POS',
                },
                client_transaction_id='offline-history-001',
            )
            sealed_snapshot = runtime.seal_active_segment()
            runtime.append_sale_event(
                event_id='evt-offline-sealed-json-2',
                payload={
                    'sale_total': '7.80',
                    'payment_status': 'PAID',
                    'payment_reference': 'OFFLINE-HISTORY-002',
                    'journal_capture_source': 'server_django_sales',
                    'capture_event_type': 'sale.payment_confirmed',
                    'sale_origin': 'POS',
                },
                client_transaction_id='offline-history-002',
            )

            self.client.force_login(self.admin_user)
            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
                OFFLINE_JOURNAL_HISTORY_LIMIT=5,
            ):
                response = self.client.get(reverse('dashboard_offline_limbo_json'))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload['sealed_segments']), 1)
        self.assertEqual(payload['sealed_segments'][0]['segment_id'], sealed_snapshot['segment_id'])
        self.assertEqual(payload['sealed_segments'][0]['status'], 'sealed')
        self.assertTrue(payload['sealed_segments'][0]['footer_present'])
        self.assertEqual(payload['sealed_segments'][0]['summary_total_sales'], 1)
        self.assertEqual(payload['sealed_segments'][0]['summary_amount_total'], '3.15')

    def test_dashboard_offline_limbo_json_rejects_non_admin_user(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('dashboard_offline_limbo_json'))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['detail'], 'admin required')

    def test_dashboard_offline_limbo_reconcile_json_repairs_lagging_snapshot(self):
        with TemporaryDirectory() as temp_dir:
            runtime = SegmentedJournalRuntime(
                config=OfflineJournalRuntimeConfig(
                    root_dir=Path(temp_dir),
                    stream_name='sales',
                )
            )
            runtime.append_sale_event(
                event_id='evt-offline-reconcile-1',
                payload={
                    'sale_total': '9.10',
                    'payment_status': 'PAID',
                    'payment_reference': 'OFFLINE-RECON-001',
                    'journal_capture_source': 'server_django_sales',
                    'capture_event_type': 'sale.payment_confirmed',
                    'sale_origin': 'POS',
                },
                client_transaction_id='offline-reconcile-001',
            )
            limbo = runtime.get_limbo_view()
            snapshot_path = Path(limbo['snapshot_path'])
            snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
            snapshot['record_count'] = 0
            snapshot['last_offset_confirmed'] = 0
            snapshot['last_event_id'] = ''
            snapshot['last_record_hash'] = ''
            snapshot['rolling_crc32'] = '00000000'
            snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')

            self.client.force_login(self.admin_user)
            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
            ):
                response = self.client.post(reverse('dashboard_offline_limbo_reconcile_json'))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['action']['name'], 'reconcile_sidecar')
        self.assertTrue(payload['action']['performed'])
        self.assertEqual(payload['limbo']['record_count'], 1)
        self.assertEqual(payload['limbo']['summary']['total_sales'], 1)
        self.assertIn('Sidecar reconciliado', payload['action']['detail'])

    def test_dashboard_offline_limbo_reseal_json_writes_pending_footer(self):
        with TemporaryDirectory() as temp_dir:
            runtime = SegmentedJournalRuntime(
                config=OfflineJournalRuntimeConfig(
                    root_dir=Path(temp_dir),
                    stream_name='sales',
                )
            )
            runtime.append_sale_event(
                event_id='evt-offline-reseal-1',
                payload={
                    'sale_total': '6.40',
                    'payment_status': 'PAID',
                    'payment_reference': 'OFFLINE-RESEAL-001',
                    'journal_capture_source': 'server_django_sales',
                    'capture_event_type': 'sale.payment_confirmed',
                    'sale_origin': 'POS',
                },
                client_transaction_id='offline-reseal-001',
            )
            limbo = runtime.get_limbo_view()
            segment_path = Path(limbo['segment_path'])
            snapshot_path = Path(limbo['snapshot_path'])
            journal = SegmentJournal(
                segment_path=segment_path,
                snapshot_path=snapshot_path,
                segment_id=limbo['segment_id'],
            )
            journal.prepare_seal(summary=limbo['summary'])

            self.client.force_login(self.admin_user)
            with override_settings(
                OFFLINE_JOURNAL_ENABLED=True,
                OFFLINE_JOURNAL_ROOT=temp_dir,
                OFFLINE_JOURNAL_STREAM_NAME='sales',
                OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True,
            ):
                response = self.client.post(reverse('dashboard_offline_limbo_reseal_json'))
                recovery = recover_segment_prefix(segment_path)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['action']['name'], 'reseal_segment')
        self.assertTrue(payload['action']['performed'])
        self.assertTrue(payload['limbo']['sealed'])
        self.assertIn('re-sellado', payload['action']['detail'])
        self.assertIsNotNone(recovery.footer)

    def test_dashboard_offline_limbo_action_rejects_non_admin_user(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse('dashboard_offline_limbo_reconcile_json'))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['detail'], 'admin required')


@override_settings(SECURE_SSL_REDIRECT=False)
class DeliveryClaimFlowTests(TestCase):
    def setUp(self):
        self.driver = Empleado.objects.create(
            nombre='Carlos Repartidor',
            pin='4321',
            rol='DELIVERY',
            activo=True,
            telefono='0991111111',
        )
        self.sale = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='PENDIENTE_COTIZACION',
            metodo_pago='EFECTIVO',
            total='10.00',
            cliente_nombre='Andres Gutierrez',
            telefono_cliente='0991111111',
        )

    @patch('pos.application.delivery.commands.set_quote_and_notify.delay')
    @patch('pos.application.delivery.commands.notify_order_claimed')
    def test_claim_uses_real_quoted_price_in_telegram_notification(self, mock_notify_claimed, mock_set_quote):
        token = make_delivery_claim_token(self.sale.id)

        response = self.client.post(
            reverse('delivery_claim_submit', args=[token]),
            data={'pin': '4321', 'precio': '5.00'},
        )

        self.assertEqual(response.status_code, 200)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.repartidor_asignado_id, self.driver.id)
        mock_set_quote.assert_called_once_with(self.sale.id, self.driver.id, '5.00')
        mock_notify_claimed.assert_called_once()
        args, kwargs = mock_notify_claimed.call_args
        self.assertEqual(args[0].id, self.sale.id)
        self.assertEqual(args[1].id, self.driver.id)
        self.assertEqual(kwargs['precio_envio'], Decimal('5.00'))
        self.assertContains(response, 'Envio: $5.00')

    @patch('pos.application.delivery.commands.set_quote_and_notify.delay')
    @patch('pos.application.delivery.commands.notify_order_claimed')
    def test_driver_can_register_from_claim_link_and_take_order(self, mock_notify_claimed, mock_set_quote):
        token = make_delivery_claim_token(self.sale.id)

        response = self.client.post(
            reverse('delivery_claim_submit', args=[token]),
            data={
                'flow': 'register',
                'nombre': 'Nuevo Driver',
                'telefono': '0992223334',
                'nuevo_pin': '9876',
                'precio': '4.50',
            },
        )

        self.assertEqual(response.status_code, 200)
        new_driver = Empleado.objects.get(pin='9876')
        self.assertEqual(new_driver.rol, 'DELIVERY')
        self.assertTrue(new_driver.activo)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.repartidor_asignado_id, new_driver.id)
        mock_set_quote.assert_called_once_with(self.sale.id, new_driver.id, '4.50')
        mock_notify_claimed.assert_called_once()
        self.assertContains(response, 'Nuevo Driver - Envio: $4.50')

    @patch('pos.application.delivery.commands.set_quote_and_notify.delay')
    @patch('pos.application.delivery.commands.notify_order_claimed')
    def test_pos_acceptance_does_not_mark_telegram_claim_as_taken(self, mock_notify_claimed, mock_set_quote):
        self.sale.estado = 'COCINA'
        self.sale.costo_envio = Decimal('0.00')
        self.sale.save(update_fields=['estado', 'costo_envio'])
        token = make_delivery_claim_token(self.sale.id)

        form_response = self.client.get(reverse('delivery_claim_form', args=[token]))
        self.assertEqual(form_response.status_code, 200)
        self.assertNotContains(form_response, 'Pedido ya tomado')
        self.assertNotContains(form_response, 'Pedido no disponible')

        claim_response = self.client.post(
            reverse('delivery_claim_submit', args=[token]),
            data={'pin': '4321', 'precio': '5.00'},
        )

        self.assertEqual(claim_response.status_code, 200)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.estado, 'COCINA')
        self.assertEqual(self.sale.repartidor_asignado_id, self.driver.id)
        self.assertEqual(self.sale.costo_envio, Decimal('5.00'))
        mock_set_quote.assert_not_called()
        mock_notify_claimed.assert_called_once()
        args, kwargs = mock_notify_claimed.call_args
        self.assertEqual(args[0].id, self.sale.id)
        self.assertEqual(args[1].id, self.driver.id)
        self.assertEqual(kwargs['precio_envio'], Decimal('5.00'))

    def test_claim_form_shows_blocked_message_for_in_transit_order(self):
        self.sale.estado = 'EN_CAMINO'
        self.sale.save(update_fields=['estado'])
        token = make_delivery_claim_token(self.sale.id)

        response = self.client.get(reverse('delivery_claim_form', args=[token]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Pedido no disponible')
        self.assertContains(response, 'Este pedido ya esta en camino.')


@override_settings(SECURE_SSL_REDIRECT=False)
class DeliveryInTransitFlowTests(TestCase):
    def setUp(self):
        self.driver = Empleado.objects.create(
            nombre='Carlos Repartidor',
            pin='4321',
            rol='DELIVERY',
            activo=True,
        )
        self.sale = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='COCINA',
            metodo_pago='EFECTIVO',
            total='10.00',
            cliente_nombre='Andres Gutierrez',
            telefono_cliente='0991111111',
            repartidor_asignado=self.driver,
            costo_envio='5.00',
        )

    def test_driver_can_mark_assigned_order_in_transit_with_eta(self):
        token = make_delivery_in_transit_token(self.sale.id, self.driver.id)

        response = self.client.post(
            reverse('delivery_in_transit_submit', args=[token]),
            data={'pin': '4321', 'eta_minutos': '20'},
        )

        self.assertEqual(response.status_code, 200)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.estado, 'EN_CAMINO')
        self.assertEqual(self.sale.tiempo_estimado_minutos, 20)
        self.assertIsNotNone(self.sale.salio_a_reparto_at)

    def test_driver_cannot_mark_other_drivers_order_in_transit(self):
        other_driver = Empleado.objects.create(
            nombre='Otro Repartidor',
            pin='1234',
            rol='DELIVERY',
            activo=True,
        )
        token = make_delivery_in_transit_token(self.sale.id, self.driver.id)

        response = self.client.post(
            reverse('delivery_in_transit_submit', args=[token]),
            data={'pin': '1234', 'eta_minutos': '15'},
        )

        self.assertEqual(response.status_code, 200)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.estado, 'COCINA')
        self.assertContains(response, 'no corresponde a tu pedido asignado')
        self.assertEqual(other_driver.rol, 'DELIVERY')


@override_settings(SECURE_SSL_REDIRECT=False)
class DeliveryCompletionFlowTests(TestCase):
    def setUp(self):
        self.driver = Empleado.objects.create(
            nombre='Carlos Repartidor',
            pin='4321',
            rol='DELIVERY',
            activo=True,
        )
        self.sale = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='EN_CAMINO',
            metodo_pago='EFECTIVO',
            total='10.00',
            cliente_nombre='Andres Gutierrez',
            telefono_cliente='0991111111',
            email_cliente='cliente@example.com',
            repartidor_asignado=self.driver,
            costo_envio='5.00',
            tiempo_estimado_minutos=20,
            salio_a_reparto_at=timezone.now() - timedelta(minutes=5),
        )

    def test_customer_can_report_delivery_received_via_public_endpoint(self):
        response = self.client.post(reverse('pedido_api_recibido', args=[self.sale.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')
        self.sale.refresh_from_db()
        self.assertIsNotNone(self.sale.cliente_reporto_recibido_at)

    def test_order_status_api_exposes_received_flags(self):
        self.sale.cliente_reporto_recibido_at = timezone.now()
        self.sale.save(update_fields=['cliente_reporto_recibido_at'])

        response = self.client.get(reverse('pedido_api_estado', args=[self.sale.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['cliente_reporto_recibido'])
        self.assertFalse(payload['repartidor_confirmo_entrega'])
        self.assertFalse(payload['puede_reportar_recibido'])
        self.assertTrue(payload['esperando_confirmacion_delivery'])

    @patch('pos.application.delivery.commands.send_sale_receipt_email_async')
    @patch('pos.application.delivery.commands.queue_delivery_receipt_ticket.delay')
    def test_driver_can_confirm_completed_delivery_after_customer_reports_received(
        self,
        mock_queue_ticket,
        mock_send_email,
    ):
        self.sale.cliente_reporto_recibido_at = timezone.now()
        self.sale.save(update_fields=['cliente_reporto_recibido_at'])
        token = make_delivery_delivered_token(self.sale.id, self.driver.id)

        response = self.client.post(
            reverse('delivery_delivered_submit', args=[token]),
            data={'pin': '4321'},
        )

        self.assertEqual(response.status_code, 200)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.estado, 'LISTO')
        self.assertIsNotNone(self.sale.repartidor_confirmo_entrega_at)
        mock_queue_ticket.assert_called_once_with(self.sale.id)
        mock_send_email.assert_called_once_with(self.sale, 'cliente@example.com')
        self.assertContains(response, 'ya fue confirmada')

    @patch('pos.application.delivery.commands.send_sale_receipt_email_async')
    @patch('pos.application.delivery.commands.queue_delivery_receipt_ticket.delay')
    def test_driver_cannot_confirm_completed_delivery_before_customer_reports_received(
        self,
        mock_queue_ticket,
        mock_send_email,
    ):
        token = make_delivery_delivered_token(self.sale.id, self.driver.id)

        response = self.client.post(
            reverse('delivery_delivered_submit', args=[token]),
            data={'pin': '4321'},
        )

        self.assertEqual(response.status_code, 200)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.estado, 'EN_CAMINO')
        self.assertIsNone(self.sale.repartidor_confirmo_entrega_at)
        mock_queue_ticket.assert_not_called()
        mock_send_email.assert_not_called()
        self.assertContains(response, 'cliente aun no marca el pedido como recibido')


@override_settings(SECURE_SSL_REDIRECT=False)
class CustomerOrderConfirmationEtaTests(TestCase):
    def test_confirmation_page_shows_remaining_eta_when_order_in_transit(self):
        sale = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='EN_CAMINO',
            metodo_pago='EFECTIVO',
            total='10.00',
            cliente_nombre='Cliente Demo',
            telefono_cliente='0991111111',
            costo_envio='2.50',
            tiempo_estimado_minutos=20,
            salio_a_reparto_at=timezone.now() - timedelta(minutes=5),
        )

        response = self.client.get(reverse('pedido_confirmacion', args=[sale.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Tiempo estimado de llegada: 15 min')

    def test_order_status_api_includes_assigned_driver_name(self):
        driver = Empleado.objects.create(
            nombre='Carlos Repartidor',
            pin='4455',
            rol='DELIVERY',
            activo=True,
        )
        sale = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='EN_CAMINO',
            metodo_pago='EFECTIVO',
            total='10.00',
            cliente_nombre='Cliente Demo',
            telefono_cliente='0991111111',
            costo_envio='2.50',
            tiempo_estimado_minutos=20,
            salio_a_reparto_at=timezone.now(),
            repartidor_asignado=driver,
        )

        response = self.client.get(reverse('pedido_api_estado', args=[sale.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['repartidor_nombre'], 'Carlos Repartidor')


@override_settings(SECURE_SSL_REDIRECT=False)
class SaleReceiptEmailTests(TestCase):
    def setUp(self):
        self.sale = Venta.objects.create(
            origen='WEB',
            tipo_pedido='DOMICILIO',
            estado='LISTO',
            metodo_pago='TRANSFERENCIA',
            total=Decimal('10.00'),
            cliente_nombre='Cliente Demo',
            telefono_cliente='0991111111',
            email_cliente='cliente@example.com',
            costo_envio=Decimal('2.50'),
        )

    @override_settings(
        RESEND_API_KEY='re_test_123',
        DEFAULT_FROM_EMAIL='RAMON by Bosco <onboarding@resend.dev>',
        RESEND_API_BASE='https://api.resend.com',
        RESEND_API_TIMEOUT_SECONDS=15,
    )
    @patch('pos.infrastructure.notifications.email.urlrequest.urlopen')
    def test_sale_receipt_uses_resend_api_when_api_key_exists(self, mock_urlopen):
        mock_response = io.BytesIO(b'{"id":"email_123"}')
        mock_context = type('Ctx', (), {
            '__enter__': lambda self: mock_response,
            '__exit__': lambda self, exc_type, exc, tb: False,
        })()
        mock_urlopen.return_value = mock_context

        send_sale_receipt_email(self.sale, 'agguti0@gmail.com')

        request_obj = mock_urlopen.call_args.args[0]
        payload = json.loads(request_obj.data.decode('utf-8'))
        self.assertEqual(payload['from'], 'RAMON by Bosco <onboarding@resend.dev>')
        self.assertEqual(payload['to'], ['agguti0@gmail.com'])
        self.assertIn('Comprobante de Venta', payload['subject'])
        self.assertIn('Bearer re_test_123', request_obj.headers['Authorization'])

    @override_settings(RESEND_API_KEY='')
    @patch('pos.application.sales.commands.send_mail')
    def test_sale_receipt_falls_back_to_django_mail_when_resend_missing(self, mock_send_mail):
        send_sale_receipt_email(self.sale, 'agguti0@gmail.com')

        mock_send_mail.assert_called_once()
        self.assertIn('Comprobante de Venta', mock_send_mail.call_args.kwargs['subject'])
        self.assertEqual(mock_send_mail.call_args.kwargs['recipient_list'], ['agguti0@gmail.com'])


@override_settings(
    WHATSAPP_PROVIDER='META',
    META_WHATSAPP_VERIFY_TOKEN='verify-token-demo',
    META_SIGNATURE_VALIDATION=False,
    CELERY_TASK_ALWAYS_EAGER=True,
    SECURE_SSL_REDIRECT=False,
)
class MetaWebhookTests(TestCase):
    def test_meta_webhook_verification_get(self):
        url = reverse('whatsapp_webhook')
        resp = self.client.get(
            url,
            {
                'hub.mode': 'subscribe',
                'hub.verify_token': 'verify-token-demo',
                'hub.challenge': '12345',
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode(), '12345')

    def test_meta_webhook_inbound_text(self):
        url = reverse('whatsapp_webhook')
        payload = {
            'entry': [
                {
                    'changes': [
                        {
                            'value': {
                                'messages': [
                                    {
                                        'from': '593991112233',
                                        'id': 'wamid.TEST.001',
                                        'type': 'text',
                                        'text': {'body': 'hola'},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        resp = self.client.post(url, data=json.dumps(payload), content_type='application/json')

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            WhatsAppMessageLog.objects.filter(
                direction='IN',
                telefono_e164='+593991112233',
                message_sid='wamid.TEST.001',
            ).exists()
        )


class WebOrderApiRequestParsingTests(SimpleTestCase):
    def test_parse_web_order_request_rejects_invalid_json_payload(self):
        request = RequestFactory().post(
            '/pedido/api/crear/',
            data='{invalid json',
            content_type='application/json',
        )

        with self.assertRaisesMessage(WebOrderError, 'Payload JSON invalido'):
            parse_web_order_request(request)

    def test_parse_web_order_request_promotes_legacy_reference_to_canonical_key(self):
        request = RequestFactory().post(
            '/pedido/api/crear/',
            data=json.dumps({'nombre': 'Cliente', 'referencia_pago': 'LEGACY-WEB-001'}),
            content_type='application/json',
        )

        data, comprobante = parse_web_order_request(request)

        self.assertIsNone(comprobante)
        self.assertEqual(data['payment_reference'], 'LEGACY-WEB-001')

    def test_parse_web_order_request_preserves_canonical_payment_reference(self):
        request = RequestFactory().post(
            '/pedido/api/crear/',
            data=json.dumps({'nombre': 'Cliente', 'payment_reference': 'PAY-WEB-API-001'}),
            content_type='application/json',
        )

        data, comprobante = parse_web_order_request(request)

        self.assertIsNone(comprobante)
        self.assertEqual(data['payment_reference'], 'PAY-WEB-API-001')


class VentaAdminConfigurationTests(SimpleTestCase):
    def test_admin_uses_canonical_payment_readonly_fields(self):
        venta_admin = VentaAdmin(Venta, admin.site)

        readonly_fields = venta_admin.get_readonly_fields(request=None)

        self.assertIn('payment_status_display', readonly_fields)
        self.assertIn('payment_method_type', readonly_fields)
        self.assertIn('payment_reference', readonly_fields)
        self.assertNotIn('legacy_estado_pago_display', readonly_fields)
        self.assertNotIn('legacy_payment_reference_display', readonly_fields)

    def test_admin_excludes_legacy_payment_fields(self):
        venta_admin = VentaAdmin(Venta, admin.site)

        self.assertIn('estado_pago', venta_admin.exclude)
        self.assertIn('referencia_pago', venta_admin.exclude)


class LegacyImportRegistryTests(SimpleTestCase):
    def test_legacy_registry_covers_expected_wrappers(self):
        from pos.legacy import LEGACY_IMPORT_REDIRECTS

        self.assertEqual(LEGACY_IMPORT_REDIRECTS['pos.tasks'], 'pos.infrastructure.tasks')
        self.assertEqual(LEGACY_IMPORT_REDIRECTS['pedidos.views'], 'pos.presentation.api.public')
        self.assertEqual(LEGACY_IMPORT_REDIRECTS['pedidos.urls'], 'pos.presentation.api.urls')

    def test_legacy_registry_helper_returns_none_for_unknown_module(self):
        from pos.legacy import get_legacy_import_redirect

        self.assertIsNone(get_legacy_import_redirect('pos.nonexistent_legacy_wrapper'))

    def test_legacy_contract_exposes_retirement_metadata(self):
        from pos.legacy import get_legacy_contract

        contract = get_legacy_contract('pos.tasks')

        self.assertIsNotNone(contract)
        self.assertEqual(contract.canonical_target, 'pos.infrastructure.tasks')
        self.assertEqual(contract.compatibility_role, 'operational Celery alias')
        self.assertEqual(contract.removal_phase, 'phase_6_retire_operational_aliases')

    def test_require_legacy_contract_raises_for_unknown_module(self):
        from pos.legacy import require_legacy_contract

        with self.assertRaises(KeyError):
            require_legacy_contract('pos.unknown_wrapper')

    def test_legacy_module_file_uses_python_module_convention(self):
        from pos.legacy import get_legacy_module_file

        self.assertEqual(get_legacy_module_file('pos.tasks'), 'pos/tasks.py')
        self.assertEqual(get_legacy_module_file('pedidos.views'), 'pedidos/views.py')

    def test_legacy_wrappers_expose_registry_metadata(self):
        from pedidos import views as legacy_pedidos_views
        from pos import tasks as legacy_tasks

        self.assertEqual(legacy_tasks.LEGACY_MODULE_PATH, 'pos.tasks')
        self.assertEqual(legacy_tasks.CANONICAL_TARGET, 'pos.infrastructure.tasks')
        self.assertEqual(legacy_tasks.COMPATIBILITY_ROLE, 'operational Celery alias')
        self.assertEqual(legacy_pedidos_views.COMPATIBILITY_ROLE, 'legacy presentation alias')
        self.assertEqual(legacy_pedidos_views.REMOVAL_PHASE, 'phase_4_retire_legacy_entrypoints')

    def test_phase_4_legacy_entrypoints_emit_deprecation_warning_on_import(self):
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always', DeprecationWarning)
            legacy_pedidos_views = import_module('pedidos.views')
            importlib.reload(legacy_pedidos_views)
            legacy_pedidos_urls = import_module('pedidos.urls')
            importlib.reload(legacy_pedidos_urls)

        messages = [str(warning.message) for warning in captured]
        self.assertTrue(any('Legacy import "pedidos.views"' in message for message in messages))
        self.assertTrue(any('Legacy import "pedidos.urls"' in message for message in messages))

    def test_all_legacy_wrappers_export_uniform_metadata(self):
        from pos.legacy import iter_legacy_modules

        for module_path, contract in iter_legacy_modules():
            module = import_module(module_path)

            self.assertEqual(module.LEGACY_MODULE_PATH, module_path)
            self.assertEqual(module.LEGACY_CONTRACT, contract)
            self.assertEqual(module.CANONICAL_TARGET, contract.canonical_target)
            self.assertEqual(module.COMPATIBILITY_ROLE, contract.compatibility_role)
            self.assertEqual(module.REMOVAL_PHASE, contract.removal_phase)

    def test_audit_legacy_imports_json_reports_known_modules(self):
        out = io.StringIO()

        call_command('audit_legacy_imports', '--json', stdout=out)
        payload = json.loads(out.getvalue())

        self.assertIn('summary', payload)
        self.assertIn('modules', payload)

        task_module = next(
            module for module in payload['modules'] if module['module_path'] == 'pos.tasks'
        )
        self.assertEqual(task_module['wrapper_path'], 'pos/tasks.py')
        self.assertGreater(task_module['reference_counts']['total'], 0)
        self.assertGreater(task_module['reference_counts']['registry'], 0)
        self.assertEqual(task_module['reference_counts']['code'], 0)
        self.assertFalse(task_module['retirement_candidate'])

    def test_audit_legacy_imports_marks_registry_only_modules_as_candidates(self):
        out = io.StringIO()

        call_command('audit_legacy_imports', '--json', stdout=out)
        payload = json.loads(out.getvalue())

        pedidos_views_module = next(
            module for module in payload['modules'] if module['module_path'] == 'pedidos.views'
        )
        self.assertEqual(pedidos_views_module['reference_counts']['code'], 0)
        self.assertGreater(pedidos_views_module['reference_counts']['registry'], 0)
        self.assertTrue(pedidos_views_module['retirement_candidate'])

    def test_audit_legacy_imports_does_not_count_prefix_modules_as_live_code(self):
        out = io.StringIO()

        call_command('audit_legacy_imports', '--json', stdout=out)
        payload = json.loads(out.getvalue())

        pedidos_views_module = next(
            module for module in payload['modules'] if module['module_path'] == 'pedidos.views'
        )
        self.assertEqual(pedidos_views_module['reference_counts']['code'], 0)
        self.assertTrue(pedidos_views_module['retirement_candidate'])

    def test_audit_legacy_imports_marks_public_entrypoint_aliases_as_candidates(self):
        out = io.StringIO()

        call_command('audit_legacy_imports', '--json', stdout=out)
        payload = json.loads(out.getvalue())
        summary = payload['summary']

        self.assertIn('pedidos.views', summary['candidate_module_paths'])
        self.assertIn('pedidos.urls', summary['candidate_module_paths'])
        self.assertGreaterEqual(
            summary['candidate_phase_breakdown'].get('phase_4_retire_legacy_entrypoints', 0),
            2,
        )

        pedidos_views = next(
            module for module in payload['modules'] if module['module_path'] == 'pedidos.views'
        )
        pedidos_urls = next(
            module for module in payload['modules'] if module['module_path'] == 'pedidos.urls'
        )

        self.assertTrue(pedidos_views['retirement_candidate'])
        self.assertTrue(pedidos_urls['retirement_candidate'])
        self.assertEqual(pedidos_views['reference_counts']['code'], 0)
        self.assertEqual(pedidos_urls['reference_counts']['code'], 0)

    def test_audit_legacy_imports_can_filter_candidates_only_by_phase(self):
        out = io.StringIO()

        call_command(
            'audit_legacy_imports',
            '--json',
            '--phase',
            'phase_4_retire_legacy_entrypoints',
            '--candidates-only',
            stdout=out,
        )
        payload = json.loads(out.getvalue())

        self.assertTrue(payload['summary']['filters']['candidates_only'])
        self.assertEqual(
            payload['summary']['filters']['removal_phase'],
            'phase_4_retire_legacy_entrypoints',
        )
        self.assertGreaterEqual(payload['summary']['retirement_candidates'], 1)
        self.assertTrue(payload['modules'])
        self.assertTrue(
            all(module['retirement_candidate'] for module in payload['modules'])
        )
        self.assertTrue(
            all(
                module['removal_phase'] == 'phase_4_retire_legacy_entrypoints'
                for module in payload['modules']
            )
        )
        self.assertNotIn('pos.tasks', payload['summary']['candidate_module_paths'])
        self.assertEqual(
            payload['summary']['module_status_breakdown'].get('candidate'),
            len(payload['modules']),
        )

    def test_plan_legacy_retirement_groups_candidates_by_phase(self):
        out = io.StringIO()

        call_command('plan_legacy_retirement', '--json', stdout=out)
        payload = json.loads(out.getvalue())

        self.assertIn('summary', payload)
        self.assertIn('phases', payload)
        self.assertIn('phase_4_retire_legacy_entrypoints', payload['phases'])
        self.assertGreaterEqual(payload['summary']['candidate_modules'], 1)
        self.assertEqual(payload['summary']['operational_aliases'], 1)

        phase_4_modules = payload['phases']['phase_4_retire_legacy_entrypoints']['modules']
        self.assertTrue(
            any(module['module_path'] == 'pedidos.views' for module in phase_4_modules)
        )

    def test_plan_legacy_retirement_can_filter_by_phase(self):
        out = io.StringIO()

        call_command(
            'plan_legacy_retirement',
            '--json',
            '--phase',
            'phase_4_retire_legacy_entrypoints',
            stdout=out,
        )
        payload = json.loads(out.getvalue())

        self.assertEqual(
            payload['summary']['filters']['removal_phase'],
            'phase_4_retire_legacy_entrypoints',
        )
        self.assertEqual(set(payload['phases'].keys()), {'phase_4_retire_legacy_entrypoints'})
        self.assertTrue(payload['phases']['phase_4_retire_legacy_entrypoints']['modules'])
        self.assertTrue(
            all(
                module['module_path'] in {'pedidos.views', 'pedidos.urls'}
                for module in payload['phases']['phase_4_retire_legacy_entrypoints']['modules']
            )
        )

    def test_enforce_legacy_boundaries_passes_with_current_registry_state(self):
        out = io.StringIO()

        call_command('enforce_legacy_boundaries', stdout=out)

        self.assertIn('Legacy boundaries enforced', out.getvalue())

    def test_audit_legacy_imports_reports_warning_coverage_for_candidates(self):
        out = io.StringIO()

        call_command('audit_legacy_imports', '--json', stdout=out)
        payload = json.loads(out.getvalue())

        self.assertGreaterEqual(payload['summary']['warning_enabled_candidates'], 1)
        self.assertEqual(payload['summary']['warning_missing_candidates'], 0)

        pedidos_views = next(
            module for module in payload['modules'] if module['module_path'] == 'pedidos.views'
        )

        self.assertTrue(pedidos_views['warning_enabled'])
        self.assertEqual(payload['summary']['warning_enabled_candidates'], 2)

    def test_verify_legacy_deprecations_passes_with_current_registry_state(self):
        out = io.StringIO()

        call_command('verify_legacy_deprecations', stdout=out)

        self.assertIn('Legacy deprecations verified', out.getvalue())
