from __future__ import annotations

import json
from collections import defaultdict
from io import StringIO

from django.core.management import BaseCommand, call_command

from pos.legacy import get_removal_phase_description


class Command(BaseCommand):
    help = 'Build a phased retirement plan from the legacy import audit.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--json',
            action='store_true',
            help='Emit the retirement plan as JSON.',
        )
        parser.add_argument(
            '--phase',
            help='Limit the plan to a specific removal phase.',
        )

    def handle(self, *args, **options):
        payload = self._build_plan(removal_phase=options.get('phase'))
        if options.get('json'):
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
            return

        self._write_human_output(payload)

    def _build_plan(self, *, removal_phase: str | None = None) -> dict:
        audit = self._load_audit_payload(removal_phase=removal_phase)
        phases: dict[str, dict] = {}
        candidate_modules = defaultdict(list)
        operational_aliases = []

        for module in audit['modules']:
            if module['retirement_candidate']:
                candidate_modules[module['removal_phase']].append(
                    {
                        'module_path': module['module_path'],
                        'canonical_target': module['canonical_target'],
                        'compatibility_role': module['compatibility_role'],
                        'notes': module['notes'],
                    }
                )
            elif module['retirement_status'] == 'operational_alias':
                operational_aliases.append(
                    {
                        'module_path': module['module_path'],
                        'canonical_target': module['canonical_target'],
                        'compatibility_role': module['compatibility_role'],
                        'removal_phase': module['removal_phase'],
                        'notes': module['notes'],
                    }
                )

        for phase_name, modules in sorted(candidate_modules.items()):
            phases[phase_name] = {
                'description': get_removal_phase_description(phase_name),
                'modules': sorted(modules, key=lambda item: item['module_path']),
            }

        return {
            'summary': {
                'filters': audit['summary']['filters'],
                'candidate_modules': audit['summary']['retirement_candidates'],
                'active_legacy_modules': audit['summary']['active_modules'],
                'operational_aliases': len(operational_aliases),
                'phases': len(phases),
            },
            'phases': phases,
            'operational_aliases': sorted(
                operational_aliases, key=lambda item: item['module_path']
            ),
        }

    def _load_audit_payload(self, *, removal_phase: str | None = None) -> dict:
        stdout = StringIO()
        args = ['audit_legacy_imports', '--json']
        if removal_phase:
            args.extend(['--phase', removal_phase])
        call_command(*args, stdout=stdout)
        return json.loads(stdout.getvalue())

    def _write_human_output(self, payload: dict) -> None:
        summary = payload['summary']
        self.stdout.write(
            self.style.SUCCESS(
                'Legacy retirement plan ready: '
                f"{summary['candidate_modules']} candidate module(s) across "
                f"{summary['phases']} phase(s)."
            )
        )
        if summary['filters']['removal_phase']:
            self.stdout.write(
                f"  phase filter: {summary['filters']['removal_phase']}"
            )

        for phase_name, phase_payload in payload['phases'].items():
            self.stdout.write(f'\n{phase_name}')
            if phase_payload['description']:
                self.stdout.write(f"  {phase_payload['description']}")
            for module in phase_payload['modules']:
                self.stdout.write(
                    f"  - {module['module_path']} -> {module['canonical_target']}"
                )
                if module['notes']:
                    self.stdout.write(f"    notes: {module['notes']}")

        if payload['operational_aliases']:
            self.stdout.write('\nOperational aliases still blocked from retirement:')
            for module in payload['operational_aliases']:
                self.stdout.write(
                    f"  - {module['module_path']} ({module['removal_phase']})"
                )
                if module['notes']:
                    self.stdout.write(f"    notes: {module['notes']}")
