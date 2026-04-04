from __future__ import annotations

import json
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from pos.application.accounting import get_organization_ledger_snapshot, reconcile_organization_ledger_counters
from pos.models import (
    AccountingAdjustment,
    AuditLog,
    Location,
    Organization,
    OrganizationLedgerCounterShard,
    OrganizationLedgerState,
    ensure_system_ledger_account,
    get_open_accounting_adjustment_total,
)


class OrganizationLedgerShardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ledger-shards', password='1234')
        self.organization = Organization.objects.create(slug='ledger-shards-org', name='Ledger Shards Org')
        self.location = Location.objects.create(
            organization=self.organization,
            slug='principal',
            name='Principal',
        )
        self.gateway_account = ensure_system_ledger_account(
            organization=self.organization,
            system_code=AccountingAdjustment.SystemLedgerCode.PAYMENT_GATEWAY_CLEARING,
        )
        self.unidentified_account = ensure_system_ledger_account(
            organization=self.organization,
            system_code=AccountingAdjustment.SystemLedgerCode.UNIDENTIFIED_RECEIPTS,
        )
        self.refund_account = ensure_system_ledger_account(
            organization=self.organization,
            system_code=AccountingAdjustment.SystemLedgerCode.REFUND_PAYABLE,
        )

    def _make_alert(self, correlation_id: str, payment_reference: str) -> AuditLog:
        return AuditLog.objects.create(
            organization=self.organization,
            location=self.location,
            actor_user=self.user,
            event_type='sale.orphan_payment_detected',
            target_model='Venta',
            target_id=correlation_id,
            payload_json={'payment_reference': payment_reference},
            correlation_id=correlation_id,
        )

    def _create_adjustment(self, *, correlation_id: str, amount: str = '25.00') -> AccountingAdjustment:
        return AccountingAdjustment.objects.create(
            organization=self.organization,
            location=self.location,
            source_audit_log=self._make_alert(correlation_id, correlation_id),
            adjustment_type=AccountingAdjustment.AdjustmentType.ORPHAN_PAYMENT_UNIDENTIFIED,
            account_bucket=AccountingAdjustment.AccountBucket.PENDING_IDENTIFICATION,
            source_account=self.gateway_account,
            destination_account=self.unidentified_account,
            status=AccountingAdjustment.Status.OPEN,
            amount=Decimal(amount),
            operating_day=timezone.localdate(),
            payment_reference=correlation_id,
            payment_provider='TEST',
            correlation_id=correlation_id,
            created_by=self.user,
        )

    def test_open_adjustment_assigns_shard_and_updates_counters(self):
        adjustment = self._create_adjustment(correlation_id='adj-open-001')

        state = OrganizationLedgerState.objects.get(organization=self.organization)
        shard = OrganizationLedgerCounterShard.objects.get(
            organization=self.organization,
            shard_id=adjustment.contingency_shard_id,
        )

        self.assertEqual(state.shard_count, 16)
        self.assertIsNotNone(adjustment.adjustment_uid)
        self.assertIsNotNone(adjustment.contingency_shard_id)
        self.assertEqual(shard.open_adjustment_count, 1)
        self.assertEqual(shard.open_adjustment_total, Decimal('25.00'))
        self.assertEqual(get_open_accounting_adjustment_total(organization=self.organization), Decimal('25.00'))

    def test_resolving_adjustment_releases_shard_counter(self):
        adjustment = self._create_adjustment(correlation_id='adj-open-002')

        adjustment.status = AccountingAdjustment.Status.RESOLVED
        adjustment.save(update_fields=['status'])

        shard = OrganizationLedgerCounterShard.objects.get(
            organization=self.organization,
            shard_id=adjustment.contingency_shard_id,
        )

        self.assertEqual(shard.open_adjustment_count, 0)
        self.assertEqual(shard.open_adjustment_total, Decimal('0.00'))
        self.assertEqual(get_open_accounting_adjustment_total(organization=self.organization), Decimal('0.00'))

    def test_reconciliation_retags_missing_shards_and_rebuilds_totals(self):
        first = self._create_adjustment(correlation_id='adj-open-003', amount='10.00')
        second = self._create_adjustment(correlation_id='adj-open-004', amount='15.00')

        AccountingAdjustment.objects.filter(pk=first.pk).update(contingency_shard_id=None)
        OrganizationLedgerCounterShard.objects.filter(organization=self.organization).update(
            open_adjustment_total=Decimal('0.00'),
            open_adjustment_count=0,
        )

        summary = reconcile_organization_ledger_counters(organization=self.organization)
        snapshot = get_organization_ledger_snapshot(organization=self.organization)

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertTrue(summary['lock_acquired'])
        self.assertEqual(summary['open_adjustment_count'], 2)
        self.assertEqual(summary['open_adjustment_total'], '25.00')
        self.assertEqual(summary['retagged_adjustment_count'], 1)
        self.assertIsNotNone(first.contingency_shard_id)
        self.assertEqual(snapshot['open_adjustment_total'], '25.00')
        self.assertEqual(snapshot['open_adjustment_count'], 2)

    def test_management_command_outputs_reconciliation_summary(self):
        self._create_adjustment(correlation_id='adj-open-005', amount='11.00')
        out = StringIO()

        call_command(
            'reconcile_ledger_shards',
            '--organization-slug',
            self.organization.slug,
            '--json',
            stdout=out,
        )
        payload = json.loads(out.getvalue())

        self.assertEqual(len(payload['organizations']), 1)
        result = payload['organizations'][0]
        self.assertEqual(result['organization_slug'], self.organization.slug)
        self.assertTrue(result['lock_acquired'])
        self.assertEqual(result['snapshot']['open_adjustment_total'], '11.00')

    def test_organization_ledger_state_rejects_shard_count_changes(self):
        self._create_adjustment(correlation_id='adj-open-006', amount='8.00')
        state = OrganizationLedgerState.objects.get(organization=self.organization)
        state.shard_count = 32

        with self.assertRaises(ValidationError):
            state.save()
