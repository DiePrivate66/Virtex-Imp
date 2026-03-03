from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import DeliveryQuote, Empleado, PrintJob, Venta, WhatsAppConversation, WhatsAppMessageLog
from .tasks import (
    process_customer_confirmation,
    process_delivery_quote_timeout,
    requeue_stuck_print_jobs,
    set_quote_and_notify,
    sweep_delivery_quote_timeouts,
)


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    TWILIO_SIGNATURE_VALIDATION=False,
    TWILIO_ACCOUNT_SID='',
    TWILIO_AUTH_TOKEN='',
    TWILIO_WHATSAPP_NUMBER='',
    SECURE_SSL_REDIRECT=False,
)
class WhatsAppWebhookTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_webhook_is_idempotent_by_message_sid(self):
        url = reverse('whatsapp_webhook')
        payload = {
            'From': 'whatsapp:+593991234567',
            'Body': 'hola',
            'MessageSid': 'SM_DUP_001',
        }

        r1 = self.client.post(url, data=payload)
        r2 = self.client.post(url, data=payload)

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(WhatsAppMessageLog.objects.filter(message_sid='SM_DUP_001').count(), 1)

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
        payload = {
            'From': 'whatsapp:+593991112233',
            'Body': 'SI',
            'MessageSid': 'SM_CONFIRM_001',
        }
        resp = self.client.post(url, data=payload)

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
        payload_base = {
            'From': 'whatsapp:+593991234568',
            'Body': 'hola',
        }
        r1 = self.client.post(url, data={**payload_base, 'MessageSid': 'SM_RL_1'})
        r2 = self.client.post(url, data={**payload_base, 'MessageSid': 'SM_RL_2'})
        r3 = self.client.post(url, data={**payload_base, 'MessageSid': 'SM_RL_3'})

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
    TWILIO_ACCOUNT_SID='',
    TWILIO_AUTH_TOKEN='',
    TWILIO_WHATSAPP_NUMBER='',
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
    TWILIO_ACCOUNT_SID='',
    TWILIO_AUTH_TOKEN='',
    TWILIO_WHATSAPP_NUMBER='',
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
        self.assertIn('twilio', payload)
        self.assertIn('delivery_quotes', payload)
        self.assertIn('print_jobs', payload)
        self.assertIn('async', payload)
        self.assertGreaterEqual(payload['delivery_quotes']['timed_out'], 1)
        self.assertGreaterEqual(payload['print_jobs']['failed'], 1)


@override_settings(
    DEBUG=True,
    CELERY_TASK_ALWAYS_EAGER=True,
    TWILIO_ACCOUNT_SID='',
    TWILIO_AUTH_TOKEN='',
    TWILIO_WHATSAPP_NUMBER='',
    REDIS_URL='redis://fake:6379/0',
    SECURE_SSL_REDIRECT=False,
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
