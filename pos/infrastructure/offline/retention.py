from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import shutil
from typing import Any

from django.utils import timezone

from .journal import load_snapshot_payload, persist_snapshot_payload, recover_segment_prefix


class OfflineRetentionError(RuntimeError):
    pass


def export_segment_bundle_to_usb(
    *,
    root_dir: Path,
    stream_name: str,
    segment_id: str,
    usb_root: Path,
    actor: str,
    reason: str,
    receipt_secret: str,
) -> dict[str, Any]:
    segment_path, snapshot_path = _resolve_segment_paths(
        root_dir=root_dir,
        stream_name=stream_name,
        segment_id=segment_id,
    )
    usb_dir = Path(usb_root) / stream_name / segment_id
    usb_dir.mkdir(parents=True, exist_ok=True)
    exported_segment_path = usb_dir / segment_path.name
    exported_snapshot_path = usb_dir / snapshot_path.name
    shutil.copy2(segment_path, exported_segment_path)
    shutil.copy2(snapshot_path, exported_snapshot_path)

    receipt = {
        'receipt_type': 'usb_export_receipt',
        'segment_id': segment_id,
        'stream_name': stream_name,
        'actor': str(actor or '').strip(),
        'reason': str(reason or '').strip()[:255],
        'created_at': timezone.now().isoformat(),
        'source_segment_path': str(segment_path),
        'source_snapshot_path': str(snapshot_path),
        'usb_segment_path': str(exported_segment_path),
        'usb_snapshot_path': str(exported_snapshot_path),
        'segment_sha256': _sha256_file(segment_path),
        'snapshot_sha256': _sha256_file(snapshot_path),
        'usb_segment_sha256': _sha256_file(exported_segment_path),
        'usb_snapshot_sha256': _sha256_file(exported_snapshot_path),
    }
    receipt['signature'] = _sign_receipt(receipt, secret=receipt_secret)
    _persist_receipt(root_dir=root_dir, receipt=receipt)
    _append_snapshot_receipt(snapshot_path=snapshot_path, receipt_type='usb_export', receipt=receipt)
    return receipt


def purge_replayed_segment_with_receipt(
    *,
    root_dir: Path,
    stream_name: str,
    segment_id: str,
    actor: str,
    reason: str,
    server_replay_receipt: str,
    receipt_secret: str,
) -> dict[str, Any]:
    if not str(server_replay_receipt or '').strip():
        raise OfflineRetentionError('server_replay_receipt requerido para purge de segmento sincronizado')
    segment_path, snapshot_path = _resolve_segment_paths(
        root_dir=root_dir,
        stream_name=stream_name,
        segment_id=segment_id,
    )
    snapshot = load_snapshot_payload(snapshot_path)
    recovery = recover_segment_prefix(segment_path)
    if not snapshot.get('sealed') and not recovery.footer:
        raise OfflineRetentionError('solo se puede purgar un segmento sellado')

    receipt = {
        'receipt_type': 'purge_receipt',
        'segment_id': segment_id,
        'stream_name': stream_name,
        'actor': str(actor or '').strip(),
        'reason': str(reason or '').strip()[:255],
        'created_at': timezone.now().isoformat(),
        'server_replay_receipt': str(server_replay_receipt or '').strip(),
        'record_count': recovery.record_count,
        'last_event_id': recovery.last_event_id,
        'last_record_hash': recovery.last_record_hash,
        'rolling_crc32': recovery.rolling_crc32,
        'segment_sha256': _sha256_file(segment_path),
        'snapshot_sha256': _sha256_file(snapshot_path),
    }
    receipt['signature'] = _sign_receipt(receipt, secret=receipt_secret)
    _persist_receipt(root_dir=root_dir, receipt=receipt)
    _append_snapshot_receipt(snapshot_path=snapshot_path, receipt_type='purge', receipt=receipt)
    segment_path.unlink(missing_ok=False)
    snapshot_path.unlink(missing_ok=False)
    return receipt


def destroy_unreplayed_segment_after_usb_export(
    *,
    root_dir: Path,
    stream_name: str,
    segment_id: str,
    actor: str,
    reason: str,
    usb_export_receipt_signature: str,
    receipt_secret: str,
    manager_override: bool,
) -> dict[str, Any]:
    if not manager_override:
        raise OfflineRetentionError(
            'purge de segmento no sincronizado bloqueado: requiere override explicito de gerente'
        )
    normalized_actor = str(actor or '').strip()
    if not normalized_actor:
        raise OfflineRetentionError('actor requerido para override de gerente')
    normalized_reason = str(reason or '').strip()
    if not normalized_reason:
        raise OfflineRetentionError('reason requerido para purge_after_usb con override de gerente')

    segment_path, snapshot_path = _resolve_segment_paths(
        root_dir=root_dir,
        stream_name=stream_name,
        segment_id=segment_id,
    )
    snapshot = load_snapshot_payload(snapshot_path)
    export_receipts = list(((snapshot.get('receipts') or {}).get('usb_export') or []))
    matching_receipt = next(
        (
            item for item in reversed(export_receipts)
            if str(item.get('signature') or '') == str(usb_export_receipt_signature or '').strip()
        ),
        None,
    )
    if not matching_receipt:
        raise OfflineRetentionError('usb_export_receipt invalido o ausente para el segmento')

    recovery = recover_segment_prefix(segment_path)
    receipt = {
        'receipt_type': 'purge_receipt',
        'purge_mode': 'manual_usb_override',
        'segment_id': segment_id,
        'stream_name': stream_name,
        'actor': normalized_actor,
        'reason': normalized_reason[:255],
        'manager_override_confirmed': True,
        'override_actor': normalized_actor,
        'override_reason': normalized_reason[:255],
        'created_at': timezone.now().isoformat(),
        'usb_export_receipt_signature': str(usb_export_receipt_signature or '').strip(),
        'record_count': recovery.record_count,
        'last_event_id': recovery.last_event_id,
        'last_record_hash': recovery.last_record_hash,
        'rolling_crc32': recovery.rolling_crc32,
        'segment_sha256': _sha256_file(segment_path),
        'snapshot_sha256': _sha256_file(snapshot_path),
    }
    receipt['signature'] = _sign_receipt(receipt, secret=receipt_secret)
    _persist_receipt(root_dir=root_dir, receipt=receipt)
    _append_snapshot_receipt(snapshot_path=snapshot_path, receipt_type='purge', receipt=receipt)
    segment_path.unlink(missing_ok=False)
    snapshot_path.unlink(missing_ok=False)
    return receipt


def _resolve_segment_paths(*, root_dir: Path, stream_name: str, segment_id: str) -> tuple[Path, Path]:
    normalized_segment_id = str(segment_id or '').strip()
    if not normalized_segment_id or not normalized_segment_id.startswith(f'{stream_name}-'):
        raise OfflineRetentionError('segment_id invalido')
    if any(separator in normalized_segment_id for separator in ('/', '\\', '..')):
        raise OfflineRetentionError('segment_id invalido')
    segment_path = Path(root_dir) / f'{normalized_segment_id}.jsonl'
    snapshot_path = Path(root_dir) / f'{normalized_segment_id}.snapshot.json'
    if not segment_path.exists():
        raise OfflineRetentionError(f'segmento inexistente: {normalized_segment_id}')
    if not snapshot_path.exists():
        raise OfflineRetentionError(f'snapshot inexistente: {normalized_segment_id}')
    return segment_path, snapshot_path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _persist_receipt(*, root_dir: Path, receipt: dict[str, Any]) -> None:
    receipts_dir = Path(root_dir) / 'receipts'
    receipts_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
    path = receipts_dir / f"{receipt['receipt_type']}-{receipt['segment_id']}-{timestamp}.json"
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=True), encoding='utf-8')


def _append_snapshot_receipt(*, snapshot_path: Path, receipt_type: str, receipt: dict[str, Any]) -> None:
    snapshot = load_snapshot_payload(snapshot_path)
    receipts = dict(snapshot.get('receipts') or {})
    normalized_key = 'usb_export' if receipt_type == 'usb_export' else 'purge'
    current = list(receipts.get(normalized_key) or [])
    current.append(receipt)
    receipts[normalized_key] = current[-50:]
    snapshot['receipts'] = receipts
    persist_snapshot_payload(snapshot_path, snapshot)


def _sign_receipt(receipt: dict[str, Any], *, secret: str) -> str:
    if not str(secret or '').strip():
        raise OfflineRetentionError('receipt_secret requerido')
    payload = {key: value for key, value in receipt.items() if key != 'signature'}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hmac.new(str(secret).encode('utf-8'), encoded, hashlib.sha256).hexdigest()
