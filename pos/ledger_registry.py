from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any


REGISTRY_VERSION = '2026.04.02-phase1'
MIN_SUPPORTED_QUEUE_SCHEMA = 1

DEFAULT_LIST_SORT_KEYS = ('code', 'system_code', 'slug', 'name', 'id')
LOCKFILE_PATH = Path(__file__).resolve().parent / 'data' / 'ledger_registry.lock.json'


SYSTEM_LEDGER_ACCOUNTS = (
    {
        'code': '1105',
        'system_code': 'PAYMENT_GATEWAY_CLEARING',
        'name': 'Cobros pasarela / banco',
        'account_type': 'ASSET',
        'immutable': True,
        'deletable': False,
    },
    {
        'code': '2105',
        'system_code': 'UNIDENTIFIED_RECEIPTS',
        'name': 'Ingresos por identificar',
        'account_type': 'LIABILITY',
        'immutable': True,
        'deletable': False,
    },
    {
        'code': '2110',
        'system_code': 'REFUND_PAYABLE',
        'name': 'Reembolsos pendientes',
        'account_type': 'LIABILITY',
        'immutable': True,
        'deletable': False,
    },
)

CONTINGENCY_ACCOUNT = {
    'system_code': 'UNIDENTIFIED_RECEIPTS',
    'bucket': 'PENDING_IDENTIFICATION',
}

LEGACY_MAPPINGS = {
    'payment_statuses': {
        'PENDIENTE': 'PENDING',
        'APROBADO': 'PAID',
        'RECHAZADO': 'FAILED',
        'ANULADO': 'VOIDED',
    },
    'account_buckets': {
        'PENDING_IDENTIFICATION': 'UNIDENTIFIED_RECEIPTS',
        'REFUND_LIABILITY': 'REFUND_PAYABLE',
    },
}

REGISTRY = {
    'registry_version': REGISTRY_VERSION,
    'min_supported_queue_schema': MIN_SUPPORTED_QUEUE_SCHEMA,
    'system_accounts': SYSTEM_LEDGER_ACCOUNTS,
    'contingency_account': CONTINGENCY_ACCOUNT,
    'legacy_mappings': LEGACY_MAPPINGS,
}


def _normalize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        return value.replace(microsecond=value.microsecond).isoformat(timespec='microseconds')
    return value.astimezone().isoformat(timespec='microseconds')


def _normalize_primitive(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat(timespec='microseconds')
    return value


def _sort_key_for_list(items: list[Any]) -> str | None:
    if not items or not all(isinstance(item, dict) for item in items):
        return None
    keys = set(items[0].keys())
    for item in items[1:]:
        keys &= set(item.keys())
    for candidate in DEFAULT_LIST_SORT_KEYS:
        if candidate in keys:
            return candidate
    return None


def canonicalize_for_hash(value: Any) -> Any:
    if isinstance(value, dict):
        normalized_items = []
        for key in sorted(value.keys()):
            normalized_items.append((str(key), canonicalize_for_hash(value[key])))
        return {key: normalized for key, normalized in normalized_items}

    if isinstance(value, (list, tuple)):
        normalized_list = [canonicalize_for_hash(item) for item in value]
        sort_key = _sort_key_for_list(normalized_list)
        if sort_key:
            normalized_list = sorted(normalized_list, key=lambda item: str(item.get(sort_key, '')))
        return normalized_list

    return _normalize_primitive(value)


def canonical_json_dumps(value: Any) -> str:
    return json.dumps(
        canonicalize_for_hash(value),
        ensure_ascii=True,
        separators=(',', ':'),
        sort_keys=False,
    )


def get_registry_snapshot() -> dict[str, Any]:
    return deepcopy(REGISTRY)


def get_registry_hash() -> str:
    payload = canonical_json_dumps(get_registry_snapshot()).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def get_system_account_definitions() -> list[dict[str, Any]]:
    return [deepcopy(item) for item in SYSTEM_LEDGER_ACCOUNTS]


def get_system_account_defaults_map() -> dict[str, dict[str, Any]]:
    return {
        item['system_code']: {
            'code': item['code'],
            'name': item['name'],
            'account_type': item['account_type'],
        }
        for item in SYSTEM_LEDGER_ACCOUNTS
    }


def build_registry_manifest(*, build_id: str = '', artifact_sha256: str = '') -> dict[str, Any]:
    return {
        'registry_version': REGISTRY_VERSION,
        'registry_hash': get_registry_hash(),
        'min_supported_queue_schema': MIN_SUPPORTED_QUEUE_SCHEMA,
        'build_id': build_id,
        'artifact_sha256': artifact_sha256,
        'registry': canonicalize_for_hash(get_registry_snapshot()),
    }


def load_registry_lockfile(path: Path | None = None) -> dict[str, Any]:
    lockfile_path = path or LOCKFILE_PATH
    return json.loads(lockfile_path.read_text(encoding='utf-8'))
