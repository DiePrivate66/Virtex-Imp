"""Audit legacy Bosco import paths that still exist for compatibility."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from pos.legacy import get_legacy_module_file, iter_legacy_modules


TEXT_FILE_SUFFIXES = {
    '.cfg',
    '.ini',
    '.json',
    '.md',
    '.py',
    '.toml',
    '.txt',
    '.yaml',
    '.yml',
}

IGNORED_DIR_NAMES = {
    '.git',
    '.hg',
    '.svn',
    '.angular',
    '__pycache__',
    'dist',
    'node_modules',
    'venv',
}


class Command(BaseCommand):
    help = 'Audit references to legacy import wrappers and flag retirement candidates.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--json',
            action='store_true',
            help='Emit the audit as JSON.',
        )
        parser.add_argument(
            '--phase',
            help='Filter modules by removal phase (for example phase_5_remove_legacy_facades).',
        )
        parser.add_argument(
            '--candidates-only',
            action='store_true',
            help='Show only wrappers that are candidates for retirement.',
        )

    def handle(self, *args, **options):
        payload = self._build_payload(
            removal_phase=options.get('phase'),
            candidates_only=options.get('candidates_only', False),
        )
        if options['json']:
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
            return

        self._write_human_output(payload)

    def _build_payload(
        self,
        *,
        removal_phase: str | None = None,
        candidates_only: bool = False,
    ) -> dict:
        base_dir = Path(settings.BASE_DIR)
        files = list(self._iter_text_files(base_dir))
        modules = []
        retirement_candidates = 0
        candidate_module_paths = []
        candidate_phase_breakdown = Counter()
        module_status_breakdown = Counter()
        warning_enabled_candidates = 0
        warning_missing_candidates = 0

        for module_path, contract in iter_legacy_modules():
            if removal_phase and contract.removal_phase != removal_phase:
                continue

            wrapper_path = get_legacy_module_file(module_path)
            warning_enabled = self._wrapper_emits_warning(base_dir, wrapper_path)
            references = self._collect_references(files, module_path, wrapper_path)
            counts = Counter(reference['classification'] for reference in references)
            counts['total'] = sum(reference['matches'] for reference in references)

            live_code_references = counts.get('code', 0) + counts.get('operational', 0)
            is_operational_alias = contract.compatibility_role == 'operational Celery alias'
            is_candidate = live_code_references == 0 and not is_operational_alias
            retirement_status = (
                'operational_alias'
                if is_operational_alias
                else 'candidate'
                if is_candidate
                else 'active'
            )
            if candidates_only and not is_candidate:
                continue

            retirement_candidates += int(is_candidate)
            if is_candidate:
                candidate_module_paths.append(module_path)
                candidate_phase_breakdown[contract.removal_phase] += 1
                if warning_enabled:
                    warning_enabled_candidates += 1
                else:
                    warning_missing_candidates += 1
            module_status_breakdown[retirement_status] += 1
            modules.append(
                {
                    'module_path': module_path,
                    'canonical_target': contract.canonical_target,
                    'compatibility_role': contract.compatibility_role,
                    'removal_phase': contract.removal_phase,
                    'notes': contract.notes,
                    'wrapper_path': wrapper_path,
                    'retirement_candidate': is_candidate,
                    'retirement_status': retirement_status,
                    'warning_enabled': warning_enabled,
                    'reference_counts': {
                        'wrapper': counts.get('wrapper', 0),
                        'tests': counts.get('tests', 0),
                        'docs': counts.get('docs', 0),
                        'registry': counts.get('registry', 0),
                        'operational': counts.get('operational', 0),
                        'code': counts.get('code', 0),
                        'total': counts['total'],
                    },
                    'references': references,
                }
            )

        return {
            'summary': {
                'modules': len(modules),
                'retirement_candidates': retirement_candidates,
                'active_modules': len(modules) - retirement_candidates,
                'candidate_module_paths': sorted(candidate_module_paths),
                'candidate_phase_breakdown': dict(sorted(candidate_phase_breakdown.items())),
                'warning_enabled_candidates': warning_enabled_candidates,
                'warning_missing_candidates': warning_missing_candidates,
                'module_status_breakdown': dict(sorted(module_status_breakdown.items())),
                'filters': {
                    'removal_phase': removal_phase,
                    'candidates_only': candidates_only,
                },
            },
            'modules': modules,
        }

    def _iter_text_files(self, base_dir: Path):
        for path in base_dir.rglob('*'):
            if not path.is_file():
                continue
            if path.suffix.lower() not in TEXT_FILE_SUFFIXES:
                continue
            if any(part in IGNORED_DIR_NAMES for part in path.parts):
                continue
            yield path

    def _wrapper_emits_warning(self, base_dir: Path, wrapper_path: str) -> bool:
        path = base_dir / Path(wrapper_path)
        try:
            content = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            return False
        return 'warn_legacy_wrapper_import(LEGACY_MODULE_PATH)' in content

    def _collect_references(self, files, module_path: str, wrapper_path: str) -> list[dict]:
        references = []
        for path in files:
            try:
                content = path.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue

            matches = self._count_module_mentions(content, module_path)
            if not matches:
                continue

            relative_path = path.relative_to(settings.BASE_DIR).as_posix()
            references.append(
                {
                    'path': relative_path,
                    'classification': self._classify_reference(relative_path, wrapper_path, module_path),
                    'matches': matches,
                }
            )
        return references

    def _count_module_mentions(self, content: str, module_path: str) -> int:
        # Match exact dotted-module references and avoid substring noise
        # across similarly named dotted paths.
        pattern = re.compile(
            rf'(?<![A-Za-z0-9_]){re.escape(module_path)}(?![A-Za-z0-9_])'
        )
        return len(pattern.findall(content))

    def _classify_reference(self, relative_path: str, wrapper_path: str, module_path: str) -> str:
        if relative_path == wrapper_path:
            return 'wrapper'
        if relative_path == 'pos/legacy.py':
            return 'registry'
        if relative_path == 'pos/management/commands/audit_legacy_imports.py':
            return 'registry'
        if module_path == 'pos.tasks':
            if relative_path == 'config/celery.py':
                return 'operational'
            if relative_path.startswith('pos/infrastructure/tasks/'):
                return 'operational'
        if relative_path.endswith('/tests.py') or '/tests/' in relative_path or relative_path.startswith('tests/'):
            return 'tests'
        if relative_path.endswith('.md') or relative_path.startswith('guides/'):
            return 'docs'
        return 'code'

    def _write_human_output(self, payload: dict) -> None:
        summary = payload['summary']
        self.stdout.write(
            self.style.SUCCESS(
                'Legacy import audit complete: '
                f"{summary['modules']} module(s), "
                f"{summary['retirement_candidates']} candidate(s) for retirement."
            )
        )
        if summary['filters']['removal_phase']:
            self.stdout.write(f"  phase filter: {summary['filters']['removal_phase']}")
        if summary['filters']['candidates_only']:
            self.stdout.write('  mode: candidates only')

        for module in payload['modules']:
            counts = module['reference_counts']
            self.stdout.write(
                f"\n{module['module_path']} -> {module['canonical_target']} [{module['retirement_status']}]"
            )
            self.stdout.write(
                '  '
                f"refs wrapper={counts['wrapper']} tests={counts['tests']} "
                f"docs={counts['docs']} registry={counts['registry']} "
                f"operational={counts['operational']} code={counts['code']} total={counts['total']} "
                f"warn={'yes' if module['warning_enabled'] else 'no'}"
            )
            if module['notes']:
                self.stdout.write(f"  notes: {module['notes']}")
