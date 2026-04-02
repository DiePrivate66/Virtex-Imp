from django.db import migrations, models
from django.db.models import Count


TENANT_GUARDED_TABLES = (
    ('cashturn', 'pos_cajaturno'),
    ('sale', 'pos_venta'),
    ('cashmove', 'pos_movimientocaja'),
    ('invmove', 'pos_movimientoinventario'),
    ('deliveryq', 'pos_deliveryquote'),
    ('printjob', 'pos_printjob'),
    ('idempo', 'pos_idempotencyrecord'),
    ('outbox', 'pos_outboxevent'),
    ('supauth', 'pos_supervisorauthorization'),
    ('audit', 'pos_auditlog'),
)


def _sqlite_trigger_sql(*, alias: str, table: str, action: str) -> str:
    trigger_name = f'pos_tenant_guard_{alias}_{action.lower()}'
    return f"""
    CREATE TRIGGER {trigger_name}
    BEFORE {action} ON {table}
    FOR EACH ROW
    WHEN NEW.location_id IS NOT NULL
      AND NEW.organization_id IS NOT NULL
      AND COALESCE((SELECT organization_id FROM pos_location WHERE id = NEW.location_id), -1) <> NEW.organization_id
    BEGIN
        SELECT RAISE(ABORT, 'tenant mismatch on {table}');
    END;
    """


def _drop_sqlite_trigger_sql(*, alias: str, action: str) -> str:
    trigger_name = f'pos_tenant_guard_{alias}_{action.lower()}'
    return f'DROP TRIGGER IF EXISTS {trigger_name};'


def dedupe_printjob_sale_type(apps, schema_editor):
    PrintJob = apps.get_model('pos', 'PrintJob')
    duplicate_pairs = (
        PrintJob.objects.values('venta_id', 'tipo')
        .annotate(row_count=Count('id'))
        .filter(row_count__gt=1)
    )
    for pair in duplicate_pairs.iterator():
        duplicate_ids = list(
            PrintJob.objects.filter(venta_id=pair['venta_id'], tipo=pair['tipo'])
            .order_by('-updated_at', '-id')
            .values_list('id', flat=True)
        )
        if len(duplicate_ids) > 1:
            PrintJob.objects.filter(id__in=duplicate_ids[1:]).delete()


def create_tenant_guard_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'sqlite':
        for alias, table in TENANT_GUARDED_TABLES:
            schema_editor.execute(_sqlite_trigger_sql(alias=alias, table=table, action='INSERT'))
            schema_editor.execute(_sqlite_trigger_sql(alias=alias, table=table, action='UPDATE'))
        return

    if vendor == 'postgresql':
        schema_editor.execute(
            """
            CREATE OR REPLACE FUNCTION pos_validate_location_org_match()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.location_id IS NOT NULL AND NEW.organization_id IS NOT NULL THEN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pos_location
                        WHERE id = NEW.location_id
                          AND organization_id = NEW.organization_id
                    ) THEN
                        RAISE EXCEPTION 'tenant mismatch on %%', TG_TABLE_NAME;
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        for alias, table in TENANT_GUARDED_TABLES:
            schema_editor.execute(f'DROP TRIGGER IF EXISTS pos_tenant_guard_{alias} ON {table};')
            schema_editor.execute(
                f"""
                CREATE TRIGGER pos_tenant_guard_{alias}
                BEFORE INSERT OR UPDATE ON {table}
                FOR EACH ROW
                EXECUTE FUNCTION pos_validate_location_org_match();
                """
            )


def drop_tenant_guard_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'sqlite':
        for alias, _table in TENANT_GUARDED_TABLES:
            schema_editor.execute(_drop_sqlite_trigger_sql(alias=alias, action='INSERT'))
            schema_editor.execute(_drop_sqlite_trigger_sql(alias=alias, action='UPDATE'))
        return

    if vendor == 'postgresql':
        for alias, table in TENANT_GUARDED_TABLES:
            schema_editor.execute(f'DROP TRIGGER IF EXISTS pos_tenant_guard_{alias} ON {table};')
        schema_editor.execute('DROP FUNCTION IF EXISTS pos_validate_location_org_match();')


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0015_location_organization_cajaturno_operating_day_and_more'),
    ]

    operations = [
        migrations.RunPython(dedupe_printjob_sale_type, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='printjob',
            constraint=models.UniqueConstraint(fields=('venta', 'tipo'), name='uq_printjob_sale_type'),
        ),
        migrations.RunPython(create_tenant_guard_triggers, drop_tenant_guard_triggers),
    ]
