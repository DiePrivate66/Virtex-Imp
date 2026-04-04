from django.db import migrations


def _sqlite_trigger_sql(*, action: str) -> str:
    trigger_name = f'pos_tenant_guard_sale_{action.lower()}'
    return f"""
    CREATE TRIGGER {trigger_name}
    BEFORE {action} ON pos_venta
    FOR EACH ROW
    WHEN NEW.location_id IS NOT NULL
      AND NEW.organization_id IS NOT NULL
      AND COALESCE((SELECT organization_id FROM pos_location WHERE id = NEW.location_id), -1) <> NEW.organization_id
    BEGIN
        SELECT RAISE(ABORT, 'tenant mismatch on pos_venta');
    END;
    """


def _drop_sqlite_trigger_sql(*, action: str) -> str:
    return f'DROP TRIGGER IF EXISTS pos_tenant_guard_sale_{action.lower()};'


def create_sale_tenant_guard_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'sqlite':
        schema_editor.execute(_drop_sqlite_trigger_sql(action='INSERT'))
        schema_editor.execute(_drop_sqlite_trigger_sql(action='UPDATE'))
        schema_editor.execute(_sqlite_trigger_sql(action='INSERT'))
        schema_editor.execute(_sqlite_trigger_sql(action='UPDATE'))
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
        schema_editor.execute('DROP TRIGGER IF EXISTS pos_tenant_guard_sale ON pos_venta;')
        schema_editor.execute(
            """
            CREATE TRIGGER pos_tenant_guard_sale
            BEFORE INSERT OR UPDATE ON pos_venta
            FOR EACH ROW
            EXECUTE FUNCTION pos_validate_location_org_match();
            """
        )


def drop_sale_tenant_guard_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'sqlite':
        schema_editor.execute(_drop_sqlite_trigger_sql(action='INSERT'))
        schema_editor.execute(_drop_sqlite_trigger_sql(action='UPDATE'))
        return

    if vendor == 'postgresql':
        schema_editor.execute('DROP TRIGGER IF EXISTS pos_tenant_guard_sale ON pos_venta;')


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0026_venta_accounting_booked_at_and_more'),
    ]

    operations = [
        migrations.RunPython(create_sale_tenant_guard_trigger, drop_sale_tenant_guard_trigger),
    ]
