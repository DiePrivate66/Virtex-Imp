import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


SYSTEM_LEDGER_ACCOUNT_DEFAULTS = {
    'PAYMENT_GATEWAY_CLEARING': {
        'code': '1105',
        'name': 'Cobros pasarela / banco',
        'account_type': 'ASSET',
    },
    'UNIDENTIFIED_RECEIPTS': {
        'code': '2105',
        'name': 'Ingresos por identificar',
        'account_type': 'LIABILITY',
    },
    'REFUND_PAYABLE': {
        'code': '2110',
        'name': 'Reembolsos pendientes',
        'account_type': 'LIABILITY',
    },
}


def _ensure_system_account(LedgerAccount, *, db_alias, organization_id, system_code):
    defaults = SYSTEM_LEDGER_ACCOUNT_DEFAULTS.get(system_code)
    if not defaults:
        raise ValueError(f'Cuenta contable de sistema desconocida: {system_code}')

    account, created = LedgerAccount.objects.using(db_alias).get_or_create(
        organization_id=organization_id,
        system_code=system_code,
        defaults={
            'code': defaults['code'],
            'name': defaults['name'],
            'account_type': defaults['account_type'],
            'active': True,
        },
    )
    if not created and not account.active:
        LedgerAccount.objects.using(db_alias).filter(pk=account.pk).update(active=True)
    return account


def forwards(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    Organization = apps.get_model('pos', 'Organization')
    AccountingAdjustment = apps.get_model('pos', 'AccountingAdjustment')
    LedgerAccount = apps.get_model('pos', 'LedgerAccount')

    for organization_id in Organization.objects.using(db_alias).values_list('id', flat=True).iterator():
        for system_code in SYSTEM_LEDGER_ACCOUNT_DEFAULTS:
            _ensure_system_account(
                LedgerAccount,
                db_alias=db_alias,
                organization_id=organization_id,
                system_code=system_code,
            )

    for adjustment in AccountingAdjustment.objects.using(db_alias).all().iterator():
        source_account = _ensure_system_account(
            LedgerAccount,
            db_alias=db_alias,
            organization_id=adjustment.organization_id,
            system_code=adjustment.source_account,
        )
        destination_account = _ensure_system_account(
            LedgerAccount,
            db_alias=db_alias,
            organization_id=adjustment.organization_id,
            system_code=adjustment.destination_account,
        )
        AccountingAdjustment.objects.using(db_alias).filter(pk=adjustment.pk).update(
            source_account_ref_id=source_account.pk,
            destination_account_ref_id=destination_account.pk,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0020_accountingadjustment_ledger_accounts'),
    ]

    operations = [
        migrations.CreateModel(
            name='LedgerAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=40)),
                ('name', models.CharField(max_length=120)),
                (
                    'account_type',
                    models.CharField(
                        choices=[
                            ('ASSET', 'Activo'),
                            ('LIABILITY', 'Pasivo'),
                            ('INCOME', 'Ingreso'),
                            ('EXPENSE', 'Gasto'),
                            ('EQUITY', 'Patrimonio'),
                        ],
                        max_length=16,
                    ),
                ),
                ('system_code', models.CharField(blank=True, db_index=True, max_length=40, null=True)),
                ('active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'organization',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='ledger_accounts',
                        to='pos.organization',
                    ),
                ),
            ],
            options={
                'ordering': ['code', 'name'],
            },
        ),
        migrations.AddConstraint(
            model_name='ledgeraccount',
            constraint=models.UniqueConstraint(fields=('organization', 'code'), name='uq_ledger_account_org_code'),
        ),
        migrations.AddConstraint(
            model_name='ledgeraccount',
            constraint=models.UniqueConstraint(
                condition=Q(system_code__isnull=False),
                fields=('organization', 'system_code'),
                name='uq_ledger_account_org_system_code',
            ),
        ),
        migrations.AddField(
            model_name='accountingadjustment',
            name='source_account_ref',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='source_accounting_adjustments',
                to='pos.ledgeraccount',
            ),
        ),
        migrations.AddField(
            model_name='accountingadjustment',
            name='destination_account_ref',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='destination_accounting_adjustments',
                to='pos.ledgeraccount',
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='accountingadjustment',
            name='source_account_ref',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='source_accounting_adjustments',
                to='pos.ledgeraccount',
            ),
        ),
        migrations.AlterField(
            model_name='accountingadjustment',
            name='destination_account_ref',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='destination_accounting_adjustments',
                to='pos.ledgeraccount',
            ),
        ),
        migrations.RemoveField(
            model_name='accountingadjustment',
            name='source_account',
        ),
        migrations.RemoveField(
            model_name='accountingadjustment',
            name='destination_account',
        ),
        migrations.RenameField(
            model_name='accountingadjustment',
            old_name='source_account_ref',
            new_name='source_account',
        ),
        migrations.RenameField(
            model_name='accountingadjustment',
            old_name='destination_account_ref',
            new_name='destination_account',
        ),
    ]
