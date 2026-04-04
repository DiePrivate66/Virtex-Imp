from __future__ import annotations

from decimal import Decimal

from django.db import connection, transaction
from django.utils import timezone

from pos.models import (
    AccountingAdjustment,
    Organization,
    OrganizationLedgerCounterShard,
    ensure_organization_ledger_state,
    compute_contingency_shard_id,
    get_open_accounting_adjustment_total,
)


LEDGER_RECONCILIATION_LOCK_NAMESPACE = 8421


def _try_acquire_organization_reconciliation_lock(*, organization_id: int) -> bool:
    if connection.vendor != 'postgresql':
        return True

    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT pg_try_advisory_xact_lock(%s, %s)',
            [LEDGER_RECONCILIATION_LOCK_NAMESPACE, int(organization_id)],
        )
        row = cursor.fetchone() or [False]
    return bool(row[0])


def get_organization_ledger_snapshot(*, organization: Organization) -> dict[str, object]:
    state = ensure_organization_ledger_state(organization=organization)
    shard_rows = list(
        OrganizationLedgerCounterShard.objects.filter(organization=organization).order_by('shard_id')
    )
    return {
        'organization_id': organization.id,
        'organization_slug': organization.slug,
        'shard_count': state.shard_count,
        'open_adjustment_total': f'{get_open_accounting_adjustment_total(organization=organization):.2f}',
        'open_adjustment_count': sum(row.open_adjustment_count for row in shard_rows),
        'shards': [
            {
                'shard_id': row.shard_id,
                'open_adjustment_total': f'{row.open_adjustment_total:.2f}',
                'open_adjustment_count': row.open_adjustment_count,
            }
            for row in shard_rows
        ],
        'last_reconciled_at': state.last_reconciled_at.isoformat() if state.last_reconciled_at else '',
        'last_reconciled_adjustment_id': state.last_reconciled_adjustment_id,
    }


@transaction.atomic
def reconcile_organization_ledger_counters(*, organization: Organization, chunk_size: int = 100) -> dict[str, object]:
    state = ensure_organization_ledger_state(organization=organization)
    lock_acquired = _try_acquire_organization_reconciliation_lock(organization_id=organization.id)
    if not lock_acquired:
        return {
            'organization_id': organization.id,
            'organization_slug': organization.slug,
            'lock_acquired': False,
            'shard_count': state.shard_count,
        }

    shard_rows = {
        row.shard_id: row
        for row in OrganizationLedgerCounterShard.objects.select_for_update().filter(
            organization=organization
        ).order_by('shard_id')
    }

    totals: dict[int, dict[str, Decimal | int]] = {
        shard_id: {
            'amount': Decimal('0.00'),
            'count': 0,
        }
        for shard_id in range(state.shard_count)
    }

    last_adjustment_id = None
    retagged_count = 0
    open_adjustment_count = 0
    open_adjustment_total = Decimal('0.00')

    queryset = (
        AccountingAdjustment.objects.select_for_update()
        .filter(
            organization=organization,
            status=AccountingAdjustment.Status.OPEN,
        )
        .order_by('effective_at', 'id')
        .only('id', 'adjustment_uid', 'amount', 'contingency_shard_id')
    )

    for adjustment in queryset.iterator(chunk_size=chunk_size):
        expected_shard_id = compute_contingency_shard_id(
            adjustment_key=adjustment.adjustment_uid.hex,
            shard_count=state.shard_count,
        )
        if adjustment.contingency_shard_id != expected_shard_id:
            AccountingAdjustment.objects.filter(pk=adjustment.pk).update(contingency_shard_id=expected_shard_id)
            adjustment.contingency_shard_id = expected_shard_id
            retagged_count += 1

        shard_bucket = totals[adjustment.contingency_shard_id]
        shard_bucket['amount'] = Decimal(shard_bucket['amount']) + adjustment.amount
        shard_bucket['count'] = int(shard_bucket['count']) + 1
        open_adjustment_total += adjustment.amount
        open_adjustment_count += 1
        last_adjustment_id = adjustment.id

    rows_to_update = []
    now = timezone.now()
    for shard_id in range(state.shard_count):
        row = shard_rows[shard_id]
        row.open_adjustment_total = Decimal(totals[shard_id]['amount']).quantize(Decimal('0.01'))
        row.open_adjustment_count = int(totals[shard_id]['count'])
        row.updated_at = now
        rows_to_update.append(row)
    if rows_to_update:
        OrganizationLedgerCounterShard.objects.bulk_update(
            rows_to_update,
            fields=['open_adjustment_total', 'open_adjustment_count', 'updated_at'],
        )

    state.last_reconciled_at = timezone.now()
    state.last_reconciled_adjustment_id = last_adjustment_id
    state.save(update_fields=['last_reconciled_at', 'last_reconciled_adjustment_id', 'updated_at'])

    return {
        'organization_id': organization.id,
        'organization_slug': organization.slug,
        'lock_acquired': True,
        'shard_count': state.shard_count,
        'open_adjustment_count': open_adjustment_count,
        'open_adjustment_total': f'{open_adjustment_total:.2f}',
        'retagged_adjustment_count': retagged_count,
        'last_reconciled_adjustment_id': last_adjustment_id,
    }
