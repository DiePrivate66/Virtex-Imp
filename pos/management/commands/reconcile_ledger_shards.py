from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from pos.application.accounting import (
    get_organization_ledger_snapshot,
    reconcile_organization_ledger_counters,
)
from pos.models import Organization


class Command(BaseCommand):
    help = 'Recalcula shards contables abiertos por organizacion y reetiqueta ajustes sin shard correcto.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--organization-slug',
            help='Reconciliar solo una organizacion.',
        )
        parser.add_argument(
            '--chunk-size',
            type=int,
            default=100,
            help='Tamano de iteracion para ajustes abiertos. Default: 100.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Salida estructurada.',
        )
        parser.add_argument(
            '--strict',
            action='store_true',
            help='Falla si no se puede tomar el lock de reconciliacion.',
        )

    def handle(self, *args, **options):
        organization_slug = (options.get('organization_slug') or '').strip()
        chunk_size = int(options.get('chunk_size') or 100)
        as_json = bool(options.get('json'))
        strict = bool(options.get('strict'))

        organizations = Organization.objects.order_by('id')
        if organization_slug:
            organizations = organizations.filter(slug=organization_slug)
            if not organizations.exists():
                raise CommandError(f'No existe una organizacion con slug "{organization_slug}".')

        results = []
        for organization in organizations.iterator():
            summary = reconcile_organization_ledger_counters(
                organization=organization,
                chunk_size=chunk_size,
            )
            summary['snapshot'] = get_organization_ledger_snapshot(organization=organization)
            results.append(summary)

        if strict and any(not result.get('lock_acquired', False) for result in results):
            raise CommandError('No se pudo obtener el lock de reconciliacion para una o mas organizaciones.')

        if as_json:
            self.stdout.write(json.dumps({'organizations': results}, indent=2, ensure_ascii=True))
            return

        for result in results:
            self.stdout.write(
                f"[ledger] org={result['organization_slug']} lock={result['lock_acquired']} "
                f"open_count={result.get('open_adjustment_count', 0)} "
                f"open_total={result.get('open_adjustment_total', '0.00')} "
                f"retagged={result.get('retagged_adjustment_count', 0)}"
            )
