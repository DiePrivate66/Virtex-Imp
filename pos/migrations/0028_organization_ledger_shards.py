from __future__ import annotations

from decimal import Decimal
import hashlib
import uuid

from django.db import migrations, models
import django.utils.timezone


DEFAULT_LEDGER_SHARD_COUNT = 16
ALLOWED_LEDGER_SHARD_COUNTS = (4, 8, 16, 32)


def _compute_shard_id(adjustment_uid, shard_count: int) -> int:
    digest = hashlib.sha256(str(adjustment_uid).encode('utf-8')).digest()
    return int.from_bytes(digest[:8], byteorder='big', signed=False) % int(shard_count)


def bootstrap_organization_ledger_shards(apps, schema_editor):
    Organization = apps.get_model('pos', 'Organization')
    AccountingAdjustment = apps.get_model('pos', 'AccountingAdjustment')
    OrganizationLedgerState = apps.get_model('pos', 'OrganizationLedgerState')
    OrganizationLedgerCounterShard = apps.get_model('pos', 'OrganizationLedgerCounterShard')

    for organization in Organization.objects.order_by('id').iterator():
        state, _ = OrganizationLedgerState.objects.get_or_create(
            organization_id=organization.id,
            defaults={'shard_count': DEFAULT_LEDGER_SHARD_COUNT},
        )
        if state.shard_count not in ALLOWED_LEDGER_SHARD_COUNTS:
            state.shard_count = DEFAULT_LEDGER_SHARD_COUNT
            state.save(update_fields=['shard_count', 'updated_at'])

        existing_shard_ids = set(
            OrganizationLedgerCounterShard.objects.filter(organization_id=organization.id).values_list('shard_id', flat=True)
        )
        missing_rows = [
            OrganizationLedgerCounterShard(organization_id=organization.id, shard_id=shard_id)
            for shard_id in range(state.shard_count)
            if shard_id not in existing_shard_ids
        ]
        if missing_rows:
            OrganizationLedgerCounterShard.objects.bulk_create(missing_rows)

        totals = {
            shard_id: {
                'amount': Decimal('0.00'),
                'count': 0,
            }
            for shard_id in range(state.shard_count)
        }

        last_adjustment_id = None
        for adjustment in AccountingAdjustment.objects.filter(organization_id=organization.id).order_by('effective_at', 'id').iterator():
            changed_fields = []
            if not adjustment.adjustment_uid:
                adjustment.adjustment_uid = uuid.uuid4()
                changed_fields.append('adjustment_uid')

            expected_shard_id = _compute_shard_id(adjustment.adjustment_uid, state.shard_count)
            if adjustment.contingency_shard_id != expected_shard_id:
                adjustment.contingency_shard_id = expected_shard_id
                changed_fields.append('contingency_shard_id')

            if changed_fields:
                adjustment.save(update_fields=changed_fields)

            if adjustment.status == 'OPEN':
                totals[expected_shard_id]['amount'] += adjustment.amount
                totals[expected_shard_id]['count'] += 1
                last_adjustment_id = adjustment.id

        for shard in OrganizationLedgerCounterShard.objects.filter(organization_id=organization.id):
            shard.open_adjustment_total = totals[shard.shard_id]['amount']
            shard.open_adjustment_count = totals[shard.shard_id]['count']
            shard.save(update_fields=['open_adjustment_total', 'open_adjustment_count', 'updated_at'])

        state.last_reconciled_at = django.utils.timezone.now()
        state.last_reconciled_adjustment_id = last_adjustment_id
        state.save(update_fields=['last_reconciled_at', 'last_reconciled_adjustment_id', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0027_refresh_sale_tenant_guard_trigger'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrganizationLedgerState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('shard_count', models.PositiveSmallIntegerField(default=16)),
                ('last_reconciled_at', models.DateTimeField(blank=True, null=True)),
                ('last_reconciled_adjustment_id', models.BigIntegerField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'organization',
                    models.OneToOneField(on_delete=models.deletion.CASCADE, related_name='ledger_state', to='pos.organization'),
                ),
            ],
            options={
                'ordering': ['organization__name'],
            },
        ),
        migrations.CreateModel(
            name='OrganizationLedgerCounterShard',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('shard_id', models.PositiveSmallIntegerField()),
                ('open_adjustment_total', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('open_adjustment_count', models.PositiveIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'organization',
                    models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='ledger_counter_shards', to='pos.organization'),
                ),
            ],
            options={
                'ordering': ['organization__name', 'shard_id'],
            },
        ),
        migrations.AddField(
            model_name='accountingadjustment',
            name='adjustment_uid',
            field=models.UUIDField(blank=True, db_index=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='accountingadjustment',
            name='contingency_shard_id',
            field=models.PositiveSmallIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='organizationledgercountershard',
            constraint=models.UniqueConstraint(fields=('organization', 'shard_id'), name='uq_org_ledger_counter_shard'),
        ),
        migrations.AddIndex(
            model_name='accountingadjustment',
            index=models.Index(fields=['organization', 'status', 'contingency_shard_id'], name='idx_adj_org_status_shard'),
        ),
        migrations.RunPython(bootstrap_organization_ledger_shards, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='accountingadjustment',
            name='adjustment_uid',
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
