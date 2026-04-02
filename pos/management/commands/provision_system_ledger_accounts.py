from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from pos.models import Organization, provision_system_ledger_accounts


class Command(BaseCommand):
    help = 'Provisiona y valida las cuentas contables de sistema definidas por ledger_registry.py.'

    def add_arguments(self, parser):
        parser.add_argument('--organization-slug', dest='organization_slug')
        parser.add_argument('--organization-id', dest='organization_id', type=int)
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args, **options):
        queryset = Organization.objects.all().order_by('id')
        if options.get('organization_id'):
            queryset = queryset.filter(id=options['organization_id'])
        if options.get('organization_slug'):
            queryset = queryset.filter(slug=options['organization_slug'])

        organizations = list(queryset)
        if not organizations:
            raise CommandError('No se encontraron organizaciones para provisionar cuentas de sistema.')

        results = []
        for organization in organizations:
            summary = provision_system_ledger_accounts(organization=organization)
            results.append(
                {
                    'organization_id': organization.id,
                    'organization_slug': organization.slug,
                    **summary,
                }
            )

        if options.get('as_json'):
            self.stdout.write(json.dumps({'organizations': results}, ensure_ascii=True, indent=2))
            return

        for result in results:
            created = ', '.join(result['created_system_codes']) or 'ninguna'
            validated = ', '.join(result['validated_system_codes']) or 'ninguna'
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{result['organization_slug']}] creadas={created} validadas={validated}"
                )
            )
