from __future__ import annotations

from io import StringIO
import json
from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from pos.ledger_registry import (
    REGISTRY_VERSION,
    build_registry_manifest,
    canonical_json_dumps,
    get_registry_hash,
    get_system_account_definitions,
    load_registry_lockfile,
)
from pos.middleware import LedgerRegistryFenceMiddleware
from pos.models import LedgerAccount, LedgerRegistryActivation, Organization


class LedgerRegistryCanonicalizationTests(SimpleTestCase):
    def test_canonical_json_is_stable_for_nested_reordering(self):
        left = {
            'registry_version': REGISTRY_VERSION,
            'system_accounts': [
                {'code': '2105', 'name': 'Ingresos por identificar', 'amount': Decimal('1.00')},
                {'code': '1105', 'name': 'Cobros pasarela / banco', 'amount': Decimal('2.50')},
            ],
            'meta': {
                'started_at': timezone.datetime(2026, 4, 2, 9, 30, 0),
                'flags': {'b': True, 'a': True},
            },
        }
        right = {
            'meta': {
                'flags': {'a': True, 'b': True},
                'started_at': timezone.datetime(2026, 4, 2, 9, 30, 0),
            },
            'system_accounts': [
                {'amount': Decimal('2.50'), 'name': 'Cobros pasarela / banco', 'code': '1105'},
                {'name': 'Ingresos por identificar', 'code': '2105', 'amount': Decimal('1.00')},
            ],
            'registry_version': REGISTRY_VERSION,
        }

        self.assertEqual(canonical_json_dumps(left), canonical_json_dumps(right))

    def test_registry_lockfile_matches_current_registry(self):
        lockfile = load_registry_lockfile()

        self.assertEqual(lockfile['registry_version'], REGISTRY_VERSION)
        self.assertEqual(lockfile['registry_hash'], get_registry_hash())
        self.assertEqual(lockfile, build_registry_manifest())


class LedgerProvisioningTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(slug='ledger-org', name='Ledger Org')

    def test_provision_command_creates_registry_accounts(self):
        out = StringIO()
        call_command(
            'provision_system_ledger_accounts',
            '--organization-slug',
            self.organization.slug,
            '--json',
            stdout=out,
        )

        payload = json.loads(out.getvalue())
        self.assertEqual(len(payload['organizations']), 1)
        self.assertEqual(
            LedgerAccount.objects.filter(organization=self.organization, system_code__isnull=False).count(),
            len(get_system_account_definitions()),
        )

    def test_system_accounts_are_immutable_by_model_contract(self):
        call_command(
            'provision_system_ledger_accounts',
            '--organization-slug',
            self.organization.slug,
            stdout=StringIO(),
        )
        account = LedgerAccount.objects.get(
            organization=self.organization,
            system_code='PAYMENT_GATEWAY_CLEARING',
        )

        account.name = 'Cuenta alterada'
        with self.assertRaises(ValidationError):
            account.save()

        account.refresh_from_db()
        with self.assertRaises(ValidationError):
            account.delete()

@override_settings(LEDGER_VERSION_FENCING_ENABLED=True, SECURE_SSL_REDIRECT=False)
class LedgerRegistryFenceMiddlewareTests(TestCase):
    def setUp(self):
        cache.clear()
        LedgerRegistryActivation.objects.all().delete()
        LedgerRegistryActivation.get_solo()
        self.factory = RequestFactory()

    def test_mutating_pos_request_requires_hash(self):
        response = self.client.post(
            '/registrar_venta/',
            data='{}',
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'ledger_identity_missing')

    def test_mutating_pos_request_rejects_unknown_hash(self):
        response = self.client.post(
            '/registrar_venta/',
            data='{}',
            content_type='application/json',
            HTTP_X_LEDGER_REGISTRY_HASH='incorrect-hash',
        )

        self.assertEqual(response.status_code, 426)
        self.assertEqual(response.json()['code'], 'ledger_registry_upgrade_required')

    def test_mutating_pos_request_drains_when_activation_hash_differs(self):
        activation = LedgerRegistryActivation.get_solo()
        activation.active_registry_hash = 'legacy-hash'
        activation.save(update_fields=['active_registry_hash', 'updated_at'])
        cache.clear()

        response = self.client.post(
            '/registrar_venta/',
            data='{}',
            content_type='application/json',
            HTTP_X_LEDGER_REGISTRY_HASH=get_registry_hash(),
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response['X-Bosco-Node-Status'], 'Draining')
        self.assertEqual(response.json()['code'], 'ledger_node_draining')

    def test_matching_hash_passes_through_to_view_and_attaches_runtime_headers(self):
        middleware = LedgerRegistryFenceMiddleware(lambda request: JsonResponse({'status': 'ok'}))
        request = self.factory.post(
            '/registrar_venta/',
            data='{}',
            content_type='application/json',
        )
        request.META['HTTP_X_LEDGER_REGISTRY_HASH'] = get_registry_hash()
        request.META['HTTP_X_POS_APP_VERSION'] = 'pos-web'
        request.META['HTTP_X_POS_QUEUE_SCHEMA'] = '1'
        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content.decode('utf-8'))['status'], 'ok')
        self.assertEqual(response['X-Ledger-Registry-Hash'], get_registry_hash())
        self.assertEqual(response['X-Ledger-Registry-Version'], REGISTRY_VERSION)
