from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse

from pos.application.context import get_default_catalog_organization
from pos.application.web_orders import WebOrderError, accept_web_order, create_web_order
from pos.models import Categoria, Location, PendingOfflineOrphanEvent, Producto, Venta


class _MockUrlopenResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._payload


@override_settings(
    SECURE_SSL_REDIRECT=False,
    PAYPHONE_ENABLED=True,
    PAYPHONE_TOKEN='pp_test_token',
    PAYPHONE_STORE_ID='store-123',
    PUBLIC_BACKEND_URL='https://example.com',
    ENABLE_BUSINESS_HOURS=False,
)
class PayPhoneWebOrdersTests(TestCase):
    def setUp(self):
        self.location = Location.get_or_create_default()
        self.categoria = Categoria.objects.create(
            nombre='PayPhone',
            organization=self.location.organization,
        )
        self.producto = Producto.objects.create(
            categoria=self.categoria,
            organization=self.location.organization,
            nombre='Combo PayPhone',
            precio=Decimal('9.50'),
            activo=True,
        )

    def _web_order_payload(self, **overrides):
        data = {
            'nombre': 'Cliente PayPhone',
            'telefono': '0991234567',
            'email': 'cliente-payphone@example.com',
            'direccion': '',
            'tipo_pedido': 'LLEVAR',
            'metodo_pago': 'PAYPHONE',
            'carrito': [{'id': self.producto.id, 'cantidad': 2, 'nombre': self.producto.nombre, 'nota': ''}],
        }
        data.update(overrides)
        return data

    def _urlopen_side_effect(self, request_obj, timeout=15):
        url = request_obj.full_url
        payload = json.loads(request_obj.data.decode('utf-8'))
        if url.endswith('/api/button/Prepare'):
            self.assertEqual(payload['clientTransactionId'][:7], 'WEBPAY-')
            self.assertEqual(payload['amount'], 1900)
            return _MockUrlopenResponse(
                {
                    'paymentId': 991,
                    'payWithCard': 'https://payphone.test/card',
                    'payWithPayPhone': 'https://payphone.test/app',
                }
            )
        if url.endswith('/api/button/V2/Confirm'):
            return _MockUrlopenResponse(
                {
                    'transactionId': 456789,
                    'clientTransactionId': payload['clientTxId'],
                    'transactionStatus': 'Approved',
                    'statusCode': 3,
                    'cardType': 'credit',
                    'cardBrand': 'VISA',
                }
            )
        raise AssertionError(f'URL inesperada en test PayPhone: {url}')

    def test_create_web_order_with_payphone_starts_pending(self):
        venta = create_web_order(self._web_order_payload())

        self.assertEqual(venta.metodo_pago, 'PAYPHONE')
        self.assertEqual(venta.payment_status, Venta.PaymentStatus.PENDING)
        self.assertEqual(venta.payment_provider, 'PAYPHONE')
        self.assertTrue(venta.client_transaction_id.startswith('WEBPAY-'))
        self.assertEqual(venta.estado, 'PENDIENTE')

    def test_create_web_order_rejects_payphone_for_delivery(self):
        with self.assertRaisesMessage(WebOrderError, 'PayPhone solo esta disponible para pedidos para llevar'):
            create_web_order(self._web_order_payload(tipo_pedido='DOMICILIO', direccion='Av. Siempre Viva'))

    @patch('pos.infrastructure.payments.payphone.urlrequest.urlopen')
    def test_create_order_api_returns_payphone_checkout_url(self, mock_urlopen):
        mock_urlopen.side_effect = self._urlopen_side_effect

        response = self.client.post(
            reverse('pedido_api_crear'),
            data={
                'nombre': 'Cliente PayPhone',
                'telefono': '0991234567',
                'email': 'cliente-payphone@example.com',
                'tipo_pedido': 'LLEVAR',
                'metodo_pago': 'PAYPHONE',
                'carrito': json.dumps([{'id': self.producto.id, 'cantidad': 2, 'nombre': self.producto.nombre, 'nota': ''}]),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['payment_status'], Venta.PaymentStatus.PENDING)
        self.assertEqual(payload['payphone_checkout_url'], 'https://payphone.test/card')

    @patch('pos.infrastructure.payments.payphone.urlrequest.urlopen')
    def test_payphone_return_endpoint_confirms_order_payment(self, mock_urlopen):
        mock_urlopen.side_effect = self._urlopen_side_effect
        venta = create_web_order(self._web_order_payload())

        response = self.client.get(
            reverse('pedido_api_payphone_return'),
            {
                'pedido_id': venta.id,
                'id': '991',
                'clientTransactionId': venta.client_transaction_id,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn('payment_result=paid', response['Location'])
        venta.refresh_from_db()
        self.assertEqual(venta.payment_status, Venta.PaymentStatus.PAID)
        self.assertEqual(venta.payment_provider, 'PAYPHONE')
        self.assertEqual(venta.payment_reference, 'PAYPHONE-456789')

    @patch('pos.application.web_orders.commands.send_sale_receipt_email_for_sale_after_commit')
    @patch('pos.infrastructure.payments.payphone.urlrequest.urlopen')
    def test_payphone_return_queues_receipt_email_after_payment(self, mock_urlopen, mock_queue_receipt):
        mock_urlopen.side_effect = self._urlopen_side_effect
        venta = create_web_order(self._web_order_payload(email='cliente-payphone@example.com'))

        response = self.client.get(
            reverse('pedido_api_payphone_return'),
            {
                'pedido_id': venta.id,
                'id': '991',
                'clientTransactionId': venta.client_transaction_id,
            },
        )

        self.assertEqual(response.status_code, 302)
        mock_queue_receipt.assert_called_once_with(venta.id)

    def test_payphone_cancel_endpoint_voids_order(self):
        venta = create_web_order(self._web_order_payload())

        response = self.client.get(
            reverse('pedido_api_payphone_cancel'),
            {'pedido_id': venta.id},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn('payment_result=cancelled', response['Location'])
        venta.refresh_from_db()
        self.assertEqual(venta.payment_status, Venta.PaymentStatus.VOIDED)
        self.assertEqual(venta.estado, 'CANCELADO')

    def test_payphone_notification_endpoint_stores_orphan_when_sale_is_missing(self):
        response = self.client.post(
            reverse('pedido_api_payphone_notify'),
            data=json.dumps(
                {
                    'ClientTransactionId': 'WEBPAY-NO-SALE',
                    'TransactionId': 'TX-001',
                    'TransactionStatus': 'Approved',
                    'StatusCode': 3,
                }
            ),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['ErrorCode'], '000')
        self.assertTrue(
            PendingOfflineOrphanEvent.objects.filter(
                event_type='payphone_notification',
                client_transaction_id='WEBPAY-NO-SALE',
            ).exists()
        )

    def test_order_status_endpoint_includes_payment_fields_for_payphone_orders(self):
        venta = create_web_order(self._web_order_payload())

        response = self.client.get(reverse('pedido_api_estado', args=[venta.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['payment_status'], Venta.PaymentStatus.PENDING)
        self.assertEqual(payload['payment_provider'], 'PAYPHONE')
        self.assertTrue(payload['payphone_checkout_pending'])

    def test_menu_page_renders_payphone_option_when_enabled(self):
        response = self.client.get(reverse('pedido_menu'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'PAYPHONE')
        self.assertContains(response, 'ch-email')

    def test_confirmation_page_renders_pending_payphone_state(self):
        venta = create_web_order(self._web_order_payload())

        response = self.client.get(reverse('pedido_confirmacion', args=[venta.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Pago pendiente')


@override_settings(SECURE_SSL_REDIRECT=False)
class PayPhoneWebOrderActionsTests(TestCase):
    def setUp(self):
        self.admin_group = Group.objects.create(name='Admin')
        self.allowed_user = User.objects.create_user(username='payphone-admin', password='1234')
        self.allowed_user.groups.add(self.admin_group)
        self.client.force_login(self.allowed_user)
        organization = get_default_catalog_organization()
        self.venta = Venta.objects.create(
            origen='WEB',
            organization=organization,
            location=Location.get_or_create_default(),
            tipo_pedido='LLEVAR',
            estado='PENDIENTE',
            metodo_pago='PAYPHONE',
            total='12.00',
            payment_status=Venta.PaymentStatus.PENDING,
            payment_provider='PAYPHONE',
            cliente_nombre='Cliente PayPhone',
            telefono_cliente='0991234567',
            client_transaction_id='WEBPAY-ACTION-001',
        )

    def test_update_web_order_rejects_accept_when_payphone_payment_is_pending(self):
        response = self.client.post(
            reverse('api_actualizar_pedido'),
            data=json.dumps({'pedido_id': self.venta.id, 'accion': 'accept_order'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn('confirmar el pago', response.json()['mensaje'].lower())
        self.venta.refresh_from_db()
        self.assertEqual(self.venta.estado, 'PENDIENTE')

    def test_web_orders_panel_renders_pending_payphone_badges(self):
        response = self.client.get(reverse('panel_pedidos_web'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'PAYPHONE')
        self.assertContains(response, 'ESPERANDO PAGO')


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, SECURE_SSL_REDIRECT=False)
class WebOrderReceiptEmailTests(TestCase):
    def setUp(self):
        self.location = Location.get_or_create_default()
        self.categoria = Categoria.objects.create(
            nombre='Comprobantes',
            organization=self.location.organization,
        )
        self.producto = Producto.objects.create(
            categoria=self.categoria,
            organization=self.location.organization,
            nombre='Combo Comprobante',
            precio=Decimal('7.25'),
            activo=True,
        )

    @patch('pos.application.web_orders.commands.send_sale_receipt_email_for_sale_after_commit')
    def test_paid_takeaway_web_order_queues_receipt_email(self, mock_queue_receipt):
        venta = create_web_order(
            {
                'nombre': 'Cliente Con Email',
                'telefono': '0991234567',
                'email': 'cliente@example.com',
                'direccion': '',
                'tipo_pedido': 'LLEVAR',
                'metodo_pago': 'TRANSFERENCIA',
                'carrito': [
                    {
                        'id': self.producto.id,
                        'cantidad': 1,
                        'nombre': self.producto.nombre,
                        'nota': '',
                    }
                ],
            }
        )

        self.assertEqual(venta.payment_status, Venta.PaymentStatus.PAID)
        mock_queue_receipt.assert_called_once_with(venta.id)

    @patch('pos.application.web_orders.commands.send_sale_receipt_email_for_sale_after_commit')
    def test_delivery_web_order_queues_receipt_email_when_cashier_accepts(self, mock_queue_receipt):
        venta = create_web_order(
            {
                'nombre': 'Cliente Delivery',
                'telefono': '0991234567',
                'email': 'cliente-delivery@example.com',
                'direccion': 'Av. Siempre Viva',
                'tipo_pedido': 'DOMICILIO',
                'metodo_pago': 'EFECTIVO',
                'carrito': [
                    {
                        'id': self.producto.id,
                        'cantidad': 1,
                        'nombre': self.producto.nombre,
                        'nota': '',
                    }
                ],
            }
        )

        mock_queue_receipt.assert_not_called()

        accepted = accept_web_order(venta.id)

        self.assertEqual(accepted.estado, 'COCINA')
        mock_queue_receipt.assert_called_once_with(venta.id)
