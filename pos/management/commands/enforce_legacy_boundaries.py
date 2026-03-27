from __future__ import annotations

import json
from io import StringIO

from django.core.management import BaseCommand, CommandError, call_command


class Command(BaseCommand):
    help = 'Fail if legacy wrapper usage regresses beyond approved operational aliases.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--json',
            action='store_true',
            help='Emit the evaluated audit payload as JSON before exiting.',
        )

    def handle(self, *args, **options):
        payload = self._load_audit_payload()
        violations = self._collect_violations(payload)

        if options.get('json'):
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))

        if violations:
            formatted = '\n'.join(f'- {violation}' for violation in violations)
            raise CommandError(
                'Legacy boundary enforcement failed. '
                'Unexpected live legacy dependencies remain:\n'
                f'{formatted}'
            )

        summary = payload['summary']
        operational_aliases = summary['module_status_breakdown'].get('operational_alias', 0)
        self.stdout.write(
            self.style.SUCCESS(
                'Legacy boundaries enforced: '
                f"{summary['retirement_candidates']} candidate wrapper(s) remain clean and "
                f"{operational_aliases} operational alias(es) are explicitly allowed."
            )
        )

    def _load_audit_payload(self) -> dict:
        stdout = StringIO()
        call_command('audit_legacy_imports', '--json', stdout=stdout)
        return json.loads(stdout.getvalue())

    def _collect_violations(self, payload: dict) -> list[str]:
        violations: list[str] = []

        for module in payload['modules']:
            status = module['retirement_status']
            code_refs = module['reference_counts']['code']

            if status == 'active':
                violations.append(
                    f"{module['module_path']} still has {code_refs} live code reference(s) "
                    f"and is not classified as an operational alias."
                )

        return violations
