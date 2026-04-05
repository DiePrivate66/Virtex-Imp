from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.utils import timezone

from pos.infrastructure.offline import (
    JournalIntegrityError,
    OfflineJournalRuntimeConfig,
    SegmentedJournalRuntime,
    load_snapshot_payload,
    persist_snapshot_payload,
    reconcile_snapshot_with_segment,
    reseal_segment_from_snapshot,
)
from pos.infrastructure.offline.writer import journal_runtime_lock
from pos.models import AuditLog, Location, Organization, OrganizationMembership

from .queries import build_offline_limbo_payload, build_offline_segment_detail_payload


class OfflineLimboActionError(RuntimeError):
    pass


def execute_offline_limbo_action(*, action: str) -> dict:
    action_name = str(action or '').strip().lower()
    if action_name not in {'reconcile_sidecar', 'reseal_segment', 'seal_active_segment'}:
        raise OfflineLimboActionError('offline limbo action desconocida')

    enabled = bool(getattr(settings, 'OFFLINE_JOURNAL_ENABLED', False))
    if not enabled:
        raise OfflineLimboActionError('runtime offline desactivado')

    root_value = str(getattr(settings, 'OFFLINE_JOURNAL_ROOT', '') or '').strip()
    if not root_value:
        raise OfflineLimboActionError('OFFLINE_JOURNAL_ROOT no esta configurado')

    root_dir = Path(root_value)
    if not root_dir.exists() or not root_dir.is_dir():
        raise OfflineLimboActionError(f'root offline invalido: {root_dir}')

    stream_name = getattr(settings, 'OFFLINE_JOURNAL_STREAM_NAME', 'sales')
    runtime_config = OfflineJournalRuntimeConfig(
        root_dir=root_dir,
        stream_name=stream_name,
        segment_max_bytes=getattr(settings, 'OFFLINE_JOURNAL_SEGMENT_MAX_BYTES', 100 * 1024 * 1024),
        limbo_recent_limit=getattr(settings, 'OFFLINE_JOURNAL_LIMBO_RECENT_LIMIT', 50),
    )
    try:
        with journal_runtime_lock(root_dir, stream_name):
            payload = build_offline_limbo_payload()
            limbo = payload.get('limbo') or {}
            rotation = payload.get('rotation') or {}
            segment_path_value = str(limbo.get('segment_path') or '').strip()
            snapshot_path_value = str(limbo.get('snapshot_path') or '').strip()
            segment_id = str(limbo.get('segment_id') or '').strip()

            if not segment_path_value or not snapshot_path_value or not segment_id:
                raise OfflineLimboActionError('no hay segmento activo para operar')

            segment_path = Path(segment_path_value)
            snapshot_path = Path(snapshot_path_value)

            if action_name == 'reconcile_sidecar':
                snapshot = reconcile_snapshot_with_segment(
                    segment_path,
                    snapshot_path,
                    segment_id=segment_id,
                )
                performed = True
                detail = (
                    'Sidecar reconciliado con el journal activo. '
                    f'{int(snapshot.get("record_count") or 0)} records confirmados.'
                )
            elif action_name == 'reseal_segment':
                resealed = reseal_segment_from_snapshot(segment_path, snapshot_path)
                performed = bool(resealed)
                if resealed:
                    detail = 'Footer del segmento re-sellado desde el sidecar.'
                else:
                    detail = 'El segmento ya estaba sellado o no tenia footer pendiente.'
            else:
                if not rotation.get('action_allowed'):
                    raise OfflineLimboActionError(
                        str(rotation.get('reason') or 'El runtime no detecta necesidad de rotacion.')
                    )
                runtime = SegmentedJournalRuntime(config=runtime_config)
                sealed_snapshot = runtime.seal_active_segment()
                if not sealed_snapshot:
                    raise OfflineLimboActionError('no hay segmento activo para sellar')
                performed = True
                detail = (
                    'Segmento activo sellado manualmente por condicion de rotacion. '
                    f'{sealed_snapshot.get("segment_id") or segment_id}'
                )
            refreshed = build_offline_limbo_payload()
    except JournalIntegrityError as exc:
        raise OfflineLimboActionError(str(exc)) from exc

    refreshed['action'] = {
        'name': action_name,
        'performed': performed,
        'detail': detail,
    }
    return refreshed


def execute_offline_segment_action(
    *,
    action: str,
    segment_id: str,
    user,
    ip_address: str = '',
    user_agent: str = '',
) -> dict:
    action_name = str(action or '').strip().lower()
    if action_name not in {'revalidate_footer', 'mark_operational_review'}:
        raise OfflineLimboActionError('offline segment action desconocida')

    enabled = bool(getattr(settings, 'OFFLINE_JOURNAL_ENABLED', False))
    if not enabled:
        raise OfflineLimboActionError('runtime offline desactivado')

    root_value = str(getattr(settings, 'OFFLINE_JOURNAL_ROOT', '') or '').strip()
    if not root_value:
        raise OfflineLimboActionError('OFFLINE_JOURNAL_ROOT no esta configurado')

    root_dir = Path(root_value)
    if not root_dir.exists() or not root_dir.is_dir():
        raise OfflineLimboActionError(f'root offline invalido: {root_dir}')

    stream_name = getattr(settings, 'OFFLINE_JOURNAL_STREAM_NAME', 'sales')
    with journal_runtime_lock(root_dir, stream_name):
        limbo_payload = build_offline_limbo_payload()
        active_segment_id = str((limbo_payload.get('limbo') or {}).get('segment_id') or '').strip()
        active_segment_is_open = not bool((limbo_payload.get('limbo') or {}).get('sealed'))
        if active_segment_is_open and str(segment_id or '').strip() == active_segment_id:
            raise OfflineLimboActionError('la accion historica no aplica sobre el segmento activo')

        detail_payload = build_offline_segment_detail_payload(segment_id)
        snapshot_path = Path(detail_payload['snapshot_path'])
        snapshot = load_snapshot_payload(snapshot_path)
        ops_metadata = dict(snapshot.get('ops_metadata') or {})
        metadata_key = 'last_footer_revalidation' if action_name == 'revalidate_footer' else 'operational_review'
        actor_username = user.get_username() if user and hasattr(user, 'get_username') else ''
        actor_user_id = getattr(user, 'id', None)

        if action_name == 'revalidate_footer':
            ops_metadata[metadata_key] = {
                'revalidated_at': timezone.now().isoformat(),
                'revalidated_by': actor_username,
                'revalidated_by_id': actor_user_id,
                'status': detail_payload['status'],
                'footer_present': detail_payload['footer_present'],
                'detail': detail_payload['detail'],
                'segment_crc32': detail_payload['rolling_crc32'],
            }
            detail = 'Footer historico revalidado y registrado en metadata operativa.'
        else:
            ops_metadata[metadata_key] = {
                'reviewed_at': timezone.now().isoformat(),
                'reviewed_by': actor_username,
                'reviewed_by_id': actor_user_id,
                'status_at_review': detail_payload['status'],
                'detail_at_review': detail_payload['detail'],
            }
            detail = 'Revision operativa historica registrada.'

        snapshot['ops_metadata'] = ops_metadata
        persist_snapshot_payload(snapshot_path, snapshot)
        refreshed = build_offline_segment_detail_payload(segment_id)
        audit_log_payload = _record_offline_segment_audit_log(
            action_name=action_name,
            segment_id=detail_payload['segment_id'],
            detail_payload=refreshed,
            user=user,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        snapshot = load_snapshot_payload(snapshot_path)
        ops_metadata = dict(snapshot.get('ops_metadata') or {})
        metadata_entry = dict(ops_metadata.get(metadata_key) or {})
        metadata_entry['audit_log'] = audit_log_payload
        ops_metadata[metadata_key] = metadata_entry
        snapshot['ops_metadata'] = ops_metadata
        persist_snapshot_payload(snapshot_path, snapshot)
        refreshed = build_offline_segment_detail_payload(segment_id)
        refreshed['action'] = {
            'name': action_name,
            'performed': True,
            'detail': detail,
        }
        refreshed['audit_log'] = audit_log_payload
        return refreshed


def _record_offline_segment_audit_log(
    *,
    action_name: str,
    segment_id: str,
    detail_payload: dict,
    user,
    ip_address: str = '',
    user_agent: str = '',
) -> dict:
    organization, location, actor_staff = _resolve_offline_segment_audit_scope(
        detail_payload=detail_payload,
        user=user,
    )
    if not organization:
        return {
            'recorded': False,
            'detail': 'No se pudo resolver organization para AuditLog desde el segmento o el usuario.',
        }

    event_type = {
        'revalidate_footer': 'offline.segment_footer_revalidated',
        'mark_operational_review': 'offline.segment_operational_review_marked',
    }[action_name]
    relevant_ops_metadata = (
        dict((detail_payload.get('ops_metadata') or {}).get('last_footer_revalidation') or {})
        if action_name == 'revalidate_footer'
        else dict((detail_payload.get('ops_metadata') or {}).get('operational_review') or {})
    )
    audit_log = AuditLog.objects.create(
        organization=organization,
        location=location,
        actor_user=user,
        actor_staff=actor_staff,
        event_type=event_type,
        target_model='OfflineJournalSegment',
        target_id=str(segment_id),
        payload_json={
            'segment_id': str(segment_id),
            'segment_status': str(detail_payload.get('status') or ''),
            'segment_detail': str(detail_payload.get('detail') or ''),
            'footer_present': bool(detail_payload.get('footer_present')),
            'rolling_crc32': str(detail_payload.get('rolling_crc32') or ''),
            'record_count': int(detail_payload.get('record_count') or 0),
            'segment_path': str(detail_payload.get('segment_path') or ''),
            'snapshot_path': str(detail_payload.get('snapshot_path') or ''),
            'summary': dict(detail_payload.get('summary') or {}),
            'ops_metadata': relevant_ops_metadata,
        },
        ip_address=str(ip_address or '').strip() or None,
        user_agent=str(user_agent or '').strip()[:255],
        correlation_id=str(segment_id)[:64],
    )
    return {
        'recorded': True,
        'audit_log_id': audit_log.id,
        'event_type': audit_log.event_type,
        'organization_id': audit_log.organization_id,
        'location_id': audit_log.location_id,
    }


def _resolve_offline_segment_audit_scope(*, detail_payload: dict, user):
    organization = None
    location = None
    actor_staff = None

    for event in detail_payload.get('recent_events') or []:
        organization_id = event.get('organization_id')
        location_id = event.get('location_id')
        if organization_id and not organization:
            organization = Organization.objects.filter(id=organization_id).first()
        if location_id and not location:
            location = Location.objects.select_related('organization').filter(id=location_id).first()
            if location and not organization:
                organization = location.organization
        if organization:
            break

    memberships = list(
        OrganizationMembership.objects.select_related('organization', 'staff_profile')
        .filter(user=user, active=True)
    ) if getattr(user, 'is_authenticated', False) else []

    if not organization and len(memberships) == 1:
        organization = memberships[0].organization

    if organization and not actor_staff:
        matching_membership = next(
            (membership for membership in memberships if membership.organization_id == organization.id),
            None,
        )
        actor_staff = getattr(matching_membership, 'staff_profile', None) if matching_membership else None

    if location and organization and location.organization_id != organization.id:
        location = None

    return organization, location, actor_staff
