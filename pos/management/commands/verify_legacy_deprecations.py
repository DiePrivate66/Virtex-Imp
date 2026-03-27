from __future__ import annotations

import json
from io import StringIO

from django.core.management import BaseCommand, CommandError, call_command


class Command(BaseCommand):
    help = 'Fail if any retirement candidate wrapper does not emit a deprecation warning.'

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
                'Legacy deprecation verification failed. '
                'Some retirement candidates are still silent:\n'
                f'{formatted}'
            )

        summary = payload['summary']
        self.stdout.write(
            self.style.SUCCESS(
                'Legacy deprecations verified: '
                f"{summary['warning_enabled_candidates']} candidate wrapper(s) emit warnings."
            )
        )

    def _load_audit_payload(self) -> dict:
        stdout = StringIO()
        call_command('audit_legacy_imports', '--json', stdout=stdout)
        return json.loads(stdout.getvalue())

    def _collect_violations(self, payload: dict) -> list[str]:
        violations: list[str] = []
        for module in payload['modules']:
            if not module['retirement_candidate']:
                continue
            if module['warning_enabled']:
                continue
            violations.append(
                f"{module['module_path']} is a retirement candidate but does not emit "
                'warn_legacy_wrapper_import(LEGACY_MODULE_PATH).'
            )
        return violations
