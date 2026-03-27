"""Central registry of legacy Bosco import paths kept for compatibility.

This module documents historical modules that still exist as thin wrappers
while the modular-monolith refactor settles. New code should import the
canonical package APIs instead of the paths listed here.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterator


@dataclass(frozen=True)
class LegacyModuleContract:
    """Describe how a historical import path maps to the modular monolith."""

    canonical_target: str
    compatibility_role: str
    removal_phase: str
    notes: str


REMOVAL_PHASES = {
    'phase_4_retire_legacy_entrypoints': 'Retirar aliases legacy de entrada publica.',
    'phase_5_remove_legacy_facades': 'Eliminar fachadas legacy internas ya sin uso vivo.',
    'phase_6_retire_operational_aliases': 'Retirar aliases operativos tras migrar tooling y nombres historicos.',
}


LEGACY_MODULES = {
    'pedidos.urls': LegacyModuleContract(
        canonical_target='pos.presentation.api.urls',
        compatibility_role='legacy routing alias',
        removal_phase='phase_4_retire_legacy_entrypoints',
        notes='Internal routing already points at pos.presentation.api.urls; keep only while external imports still use pedidos.urls.',
    ),
    'pedidos.views': LegacyModuleContract(
        canonical_target='pos.presentation.api.public',
        compatibility_role='legacy presentation alias',
        removal_phase='phase_4_retire_legacy_entrypoints',
        notes='Internal code already points at the canonical PWA facade; preserve only while external imports still use pedidos.views.',
    ),
    'pos.tasks': LegacyModuleContract(
        canonical_target='pos.infrastructure.tasks',
        compatibility_role='operational Celery alias',
        removal_phase='phase_6_retire_operational_aliases',
        notes='Keep while historical Celery task names may still exist in workers, queues, or operational tooling.',
    ),
}

LEGACY_IMPORT_REDIRECTS = {
    module_path: contract.canonical_target
    for module_path, contract in LEGACY_MODULES.items()
}


def get_legacy_import_redirect(module_path: str) -> str | None:
    """Return the canonical replacement for a historical import path."""

    contract = LEGACY_MODULES.get(module_path)
    return None if contract is None else contract.canonical_target


def get_legacy_contract(module_path: str) -> LegacyModuleContract | None:
    """Return the compatibility contract for a historical import path."""

    return LEGACY_MODULES.get(module_path)


def require_legacy_contract(module_path: str) -> LegacyModuleContract:
    """Return a legacy contract or raise if the registry is out of sync."""

    contract = get_legacy_contract(module_path)
    if contract is None:
        raise KeyError(f'Legacy module path is not registered: {module_path}')
    return contract


def build_legacy_module_metadata(
    module_path: str,
) -> tuple[str, LegacyModuleContract, str, str, str]:
    """Return the normalized metadata tuple used by legacy wrappers.

    This keeps every compatibility facade exposing the same module-level
    constants without reimplementing the lookup boilerplate in each file.
    """

    contract = require_legacy_contract(module_path)
    return (
        module_path,
        contract,
        contract.canonical_target,
        contract.compatibility_role,
        contract.removal_phase,
    )


def get_removal_phase_description(removal_phase: str) -> str:
    """Return the documented description for a retirement phase."""

    return REMOVAL_PHASES.get(removal_phase, '')


def warn_legacy_wrapper_import(module_path: str) -> None:
    """Emit a controlled deprecation warning for a legacy wrapper import."""

    contract = require_legacy_contract(module_path)
    warnings.warn(
        (
            f'Legacy import "{module_path}" is deprecated and will be retired in '
            f'{contract.removal_phase}. Import "{contract.canonical_target}" instead.'
        ),
        DeprecationWarning,
        stacklevel=2,
    )


def get_legacy_module_file(module_path: str) -> str:
    """Return the canonical repo-relative file path for a legacy module."""

    require_legacy_contract(module_path)
    return str(PurePosixPath(*module_path.split('.'))).replace('\\', '/') + '.py'


def iter_legacy_modules() -> Iterator[tuple[str, LegacyModuleContract]]:
    """Yield legacy module paths with their compatibility contracts."""

    return iter(LEGACY_MODULES.items())


def iter_legacy_modules_by_phase(removal_phase: str) -> Iterator[tuple[str, LegacyModuleContract]]:
    """Yield legacy module paths registered for a specific retirement phase."""

    return (
        (module_path, contract)
        for module_path, contract in iter_legacy_modules()
        if contract.removal_phase == removal_phase
    )


__all__ = [
    'LEGACY_IMPORT_REDIRECTS',
    'LEGACY_MODULES',
    'LegacyModuleContract',
    'REMOVAL_PHASES',
    'build_legacy_module_metadata',
    'get_legacy_contract',
    'get_legacy_import_redirect',
    'get_legacy_module_file',
    'get_removal_phase_description',
    'iter_legacy_modules',
    'iter_legacy_modules_by_phase',
    'require_legacy_contract',
    'warn_legacy_wrapper_import',
]
