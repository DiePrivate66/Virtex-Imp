from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db.models import Avg, Count, F, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pos.infrastructure.offline import (
    OfflineJournalRuntimeConfig,
    SegmentedJournalRuntime,
    load_snapshot_payload,
    recover_segment_prefix,
)
from pos.models import AccountingAdjustment, Asistencia, AuditLog, DetalleVenta, MovimientoCaja, Venta


OFFLINE_AUDIT_EVENT_TYPE_OPTIONS = (
    ('', 'Todas'),
    ('offline.segment_footer_revalidated', 'Revalidacion footer'),
    ('offline.segment_operational_review_marked', 'Revision operativa'),
)


def _resolve_period(periodo: str, hoy, desde_param, hasta_param):
    if periodo == 'hoy':
        desde = hoy
    elif periodo == 'semana':
        desde = hoy - timedelta(days=7)
    elif periodo == 'mes':
        desde = hoy - timedelta(days=30)
    elif periodo == 'custom':
        desde = desde_param or (hoy - timedelta(days=7)).isoformat()
    else:
        desde = hoy - timedelta(days=7)

    return desde, hasta_param or hoy


def _build_previous_period_totals(hoy, desde):
    dias_periodo = (hoy - desde).days if isinstance(desde, type(hoy)) else 7
    periodo_anterior_desde = (
        (desde - timedelta(days=dias_periodo))
        if isinstance(desde, type(hoy))
        else hoy - timedelta(days=14)
    )
    periodo_anterior_hasta = desde if isinstance(desde, type(hoy)) else hoy - timedelta(days=7)
    return (
        Venta.objects.filter(
            fecha__date__gte=periodo_anterior_desde,
            fecha__date__lte=periodo_anterior_hasta,
            payment_status=Venta.PaymentStatus.PAID,
        )
        .exclude(estado='CANCELADO')
        .aggregate(t=Sum('total'))['t']
        or Decimal('0')
    )


def _build_top_products(ventas):
    return (
        DetalleVenta.objects.filter(venta__in=ventas)
        .values(nombre=F('producto__nombre'))
        .annotate(
            total_vendido=Sum('cantidad'),
            total_ingresos=Sum(F('cantidad') * F('precio_unitario')),
        )
        .order_by('-total_vendido')[:10]
    )


def _build_sales_by_hour(ventas):
    ventas_por_hora = (
        ventas.annotate(hora=ExtractHour('fecha'))
        .values('hora')
        .annotate(total=Sum('total'), cantidad=Count('id'))
        .order_by('hora')
    )
    hora_pico = None
    max_ventas_hora = 1
    for venta_hora in ventas_por_hora:
        if venta_hora['cantidad'] > max_ventas_hora:
            max_ventas_hora = venta_hora['cantidad']
            hora_pico = venta_hora['hora']
    return ventas_por_hora, hora_pico, max_ventas_hora


def _build_sales_by_day(ventas):
    ventas_por_dia = (
        ventas.annotate(dia=TruncDate('fecha'))
        .values('dia')
        .annotate(total=Sum('total'), cantidad=Count('id'))
        .order_by('dia')
    )
    mejor_dia = None
    max_venta_dia = Decimal('0')
    for venta_dia in ventas_por_dia:
        if venta_dia['total'] > max_venta_dia:
            max_venta_dia = venta_dia['total']
            mejor_dia = venta_dia['dia']
    return ventas_por_dia, mejor_dia, max_venta_dia


def _build_attendance_data(desde, hasta):
    asistencias = (
        Asistencia.objects.filter(fecha__gte=desde, fecha__lte=hasta)
        .select_related('empleado')
        .order_by('-fecha', '-hora_entrada')
    )
    data = []
    for asistencia in asistencias:
        horas = 0
        estado = 'Abierto'
        if asistencia.hora_salida:
            entrada = datetime.combine(datetime.today(), asistencia.hora_entrada)
            salida = datetime.combine(datetime.today(), asistencia.hora_salida)
            diff = salida - entrada
            horas = round(diff.total_seconds() / 3600, 2)
            estado = 'Cerrado'
        data.append(
            {
                'empleado': asistencia.empleado.nombre,
                'rol': asistencia.empleado.get_rol_display(),
                'fecha': asistencia.fecha,
                'entrada': asistencia.hora_entrada,
                'salida': asistencia.hora_salida,
                'horas': horas,
                'estado': estado,
            }
        )
    return data


def _build_offline_audited_actions(desde, hasta, *, action_type: str = '', location_id: str = ''):
    base_queryset = (
        AuditLog.objects.filter(
            event_type__in=[
                'offline.segment_footer_revalidated',
                'offline.segment_operational_review_marked',
            ],
            created_at__date__gte=desde,
            created_at__date__lte=hasta,
        )
        .select_related('organization', 'location', 'actor_user')
        .order_by('-created_at')
    )
    location_options = list(
        base_queryset.exclude(location__isnull=True)
        .values('location_id', 'location__name')
        .distinct()
        .order_by('location__name')
    )

    queryset = base_queryset
    normalized_action_type = str(action_type or '').strip()
    if normalized_action_type:
        queryset = queryset.filter(event_type=normalized_action_type)

    normalized_location_id = str(location_id or '').strip()
    if normalized_location_id.isdigit():
        queryset = queryset.filter(location_id=int(normalized_location_id))
    else:
        normalized_location_id = ''

    return {
        'queryset': queryset,
        'items': list(queryset[:10]),
        'selected_action_type': normalized_action_type,
        'selected_location_id': normalized_location_id,
        'action_type_options': OFFLINE_AUDIT_EVENT_TYPE_OPTIONS,
        'location_options': location_options,
    }


def build_analytics_dashboard_context(
    periodo: str = 'semana',
    desde_param=None,
    hasta_param=None,
    offline_action_type: str = '',
    offline_action_location: str = '',
):
    hoy = timezone.localdate()
    desde, hasta = _resolve_period(periodo, hoy, desde_param, hasta_param)

    ventas = Venta.objects.filter(
        fecha__date__gte=desde,
        fecha__date__lte=hasta,
        payment_status=Venta.PaymentStatus.PAID,
    ).exclude(
        estado='CANCELADO'
    )
    total_ventas = ventas.aggregate(t=Sum('total'))['t'] or Decimal('0')
    num_ventas = ventas.count()
    ticket_promedio = ventas.aggregate(a=Avg('total'))['a'] or Decimal('0')

    ventas_hoy = Venta.objects.filter(fecha__date=hoy, payment_status=Venta.PaymentStatus.PAID).exclude(estado='CANCELADO')
    total_hoy = ventas_hoy.aggregate(t=Sum('total'))['t'] or Decimal('0')
    num_hoy = ventas_hoy.count()

    total_anterior = _build_previous_period_totals(hoy, desde)
    crecimiento = 0
    if total_anterior > 0:
        crecimiento = round(((total_ventas - total_anterior) / total_anterior) * 100, 1)

    top_productos = _build_top_products(ventas)
    ventas_por_hora, hora_pico, max_ventas_hora = _build_sales_by_hour(ventas)
    ventas_por_dia, mejor_dia, max_venta_dia = _build_sales_by_day(ventas)

    por_metodo = (
        ventas.values('metodo_pago')
        .annotate(total=Sum('total'), cantidad=Count('id'))
        .order_by('-total')
    )
    ventas_pos = ventas.filter(origen='POS').aggregate(t=Sum('total'), c=Count('id'))
    ventas_web = ventas.filter(origen='WEB').aggregate(t=Sum('total'), c=Count('id'))

    movimientos = MovimientoCaja.objects.filter(fecha__date__gte=desde, fecha__date__lte=hasta).exclude(concepto='VENTA')
    total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0')
    total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0')
    payment_exceptions_queryset = AuditLog.objects.filter(
        event_type='sale.orphan_payment_detected',
        requires_attention=True,
        resolved_at__isnull=True,
    ).select_related('location', 'actor_user')
    payment_exceptions_open = list(payment_exceptions_queryset.order_by('-created_at')[:10])

    replay_timeline_alerts_queryset = AuditLog.objects.filter(
        event_type='sale.post_close_replay_alert',
        requires_attention=True,
        resolved_at__isnull=True,
    ).select_related('location', 'actor_user')
    replay_timeline_alerts_open = list(replay_timeline_alerts_queryset.order_by('-created_at')[:10])

    chronology_estimated_sales_count = ventas.filter(chronology_estimated=True).count()

    refund_adjustments_queryset = AccountingAdjustment.objects.filter(
        account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY,
        status=AccountingAdjustment.Status.OPEN,
    ).select_related('location', 'sale', 'created_by')
    refund_adjustments_open = list(refund_adjustments_queryset.order_by('-effective_at', '-created_at')[:10])
    refund_adjustments_open_total = (
        refund_adjustments_queryset.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    )
    offline_audit_bundle = _build_offline_audited_actions(
        desde,
        hasta,
        action_type=offline_action_type,
        location_id=offline_action_location,
    )

    return {
        'periodo': periodo,
        'desde': desde,
        'hasta': hasta,
        'periods': [('hoy', 'Hoy'), ('semana', '7 dias'), ('mes', '30 dias')],
        'total_ventas': total_ventas,
        'num_ventas': num_ventas,
        'ticket_promedio': ticket_promedio,
        'total_hoy': total_hoy,
        'num_hoy': num_hoy,
        'crecimiento': crecimiento,
        'total_anterior': total_anterior,
        'top_productos': top_productos,
        'ventas_por_hora': ventas_por_hora,
        'ventas_por_dia': ventas_por_dia,
        'hora_pico': hora_pico,
        'max_ventas_hora': max_ventas_hora,
        'mejor_dia': mejor_dia,
        'max_venta_dia': max_venta_dia,
        'por_metodo': por_metodo,
        'ventas_pos': ventas_pos,
        'ventas_web': ventas_web,
        'total_egresos': total_egresos,
        'total_ingresos_extra': total_ingresos,
        'ganancia_estimada': total_ventas + total_ingresos - total_egresos,
        'asistencias': _build_attendance_data(desde, hasta),
        'payment_exceptions_open': payment_exceptions_open,
        'payment_exceptions_open_count': payment_exceptions_queryset.count(),
        'replay_timeline_alerts_open': replay_timeline_alerts_open,
        'replay_timeline_alerts_open_count': replay_timeline_alerts_queryset.count(),
        'chronology_estimated_sales_count': chronology_estimated_sales_count,
        'refund_adjustments_open': refund_adjustments_open,
        'refund_adjustments_open_count': refund_adjustments_queryset.count(),
        'refund_adjustments_open_total': refund_adjustments_open_total,
        'offline_audited_actions': offline_audit_bundle['items'],
        'offline_audited_actions_count': offline_audit_bundle['queryset'].count(),
        'offline_audited_action_filter_type': offline_audit_bundle['selected_action_type'],
        'offline_audited_action_filter_location': offline_audit_bundle['selected_location_id'],
        'offline_audited_action_type_options': offline_audit_bundle['action_type_options'],
        'offline_audited_action_location_options': offline_audit_bundle['location_options'],
    }


def build_offline_limbo_context() -> dict:
    enabled = bool(getattr(settings, 'OFFLINE_JOURNAL_ENABLED', False))
    root_value = str(getattr(settings, 'OFFLINE_JOURNAL_ROOT', '') or '').strip()
    stream_name = getattr(settings, 'OFFLINE_JOURNAL_STREAM_NAME', 'sales')
    capture_enabled = bool(getattr(settings, 'OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS', False))
    recent_limit = max(1, int(getattr(settings, 'OFFLINE_JOURNAL_LIMBO_RECENT_LIMIT', 50)))
    history_limit = max(1, int(getattr(settings, 'OFFLINE_JOURNAL_HISTORY_LIMIT', 5)))
    segment_max_bytes = int(getattr(settings, 'OFFLINE_JOURNAL_SEGMENT_MAX_BYTES', 100 * 1024 * 1024))

    context = {
        'offline_journal_enabled': enabled,
        'offline_capture_enabled': capture_enabled,
        'stream_name': stream_name,
        'root_dir': root_value,
        'status': 'disabled' if not enabled else 'ready',
        'detail': '',
        'limbo': {
            'segment_id': '',
            'segment_path': '',
            'snapshot_path': '',
            'record_count': 0,
            'sealed': False,
            'summary': {
                'total_sales': 0,
                'amount_total': '0.00',
                'recent_sales': [],
            },
        },
        'segment_health': {
            'truncated_tail': False,
            'corrupted_tail': False,
            'error_message': '',
        },
        'rotation': {
            'segment_size_bytes': 0,
            'segment_max_bytes': segment_max_bytes,
            'seal_pending': False,
            'rotation_needed': False,
            'action_allowed': False,
            'reason': 'No hay segmento activo para evaluar rotacion.',
        },
        'recent_events': [],
        'sealed_segments': [],
    }

    if not enabled:
        context['detail'] = 'Runtime offline desactivado.'
        return context
    if not root_value:
        context['status'] = 'misconfigured'
        context['detail'] = 'OFFLINE_JOURNAL_ROOT no esta configurado.'
        return context

    root_dir = Path(root_value)
    if not root_dir.exists():
        context['status'] = 'missing_root'
        context['detail'] = f'Root offline inexistente: {root_dir}'
        return context
    if not root_dir.is_dir():
        context['status'] = 'invalid_root'
        context['detail'] = f'Root offline no es un directorio: {root_dir}'
        return context

    try:
        runtime = SegmentedJournalRuntime(
            config=OfflineJournalRuntimeConfig(
                root_dir=root_dir,
                stream_name=stream_name,
                segment_max_bytes=getattr(settings, 'OFFLINE_JOURNAL_SEGMENT_MAX_BYTES', 100 * 1024 * 1024),
                limbo_recent_limit=recent_limit,
            )
        )
        limbo = runtime.get_limbo_view()
        context['limbo'] = limbo
        context['sealed_segments'] = _build_sealed_segment_history(
            root_dir=root_dir,
            stream_name=stream_name,
            active_segment_id=(
                str(limbo.get('segment_id') or '').strip()
                if not bool(limbo.get('sealed'))
                else ''
            ),
            limit=history_limit,
        )
        segment_path_value = str(limbo.get('segment_path') or '').strip()
        if not segment_path_value:
            context['status'] = 'empty'
            context['detail'] = 'No hay segmentos activos para este stream.'
            return context

        segment_path = Path(segment_path_value)
        snapshot_path = Path(str(limbo.get('snapshot_path') or '').strip())
        snapshot = load_snapshot_payload(snapshot_path)
        recovery = recover_segment_prefix(segment_path)
        context['segment_health'] = {
            'truncated_tail': recovery.truncated_tail,
            'corrupted_tail': recovery.corrupted_tail,
            'error_message': recovery.error_message,
        }
        context['rotation'] = _build_rotation_status(
            segment_path=segment_path,
            snapshot=snapshot,
            recovery=recovery,
            segment_max_bytes=segment_max_bytes,
        )
        context['recent_events'] = _build_recent_offline_events(recovery.records, limit=recent_limit)
        if recovery.truncated_tail or recovery.corrupted_tail:
            context['status'] = 'integrity_error'
            context['detail'] = recovery.error_message or 'El segmento activo tiene una cola invalida.'
            context['rotation']['action_allowed'] = False
            return context

        context['detail'] = 'Limbo cargado desde el runtime offline.'
        return context
    except Exception as exc:
        context['status'] = 'error'
        context['detail'] = str(exc)
        return context


def _build_recent_offline_events(records, *, limit: int) -> list[dict]:
    events: list[dict] = []
    for record in reversed(tuple(records)[-max(1, limit):]):
        payload = record.get('payload') or {}
        created_at = parse_datetime(str(record.get('created_at') or ''))
        events.append(
            {
                'event_id': str(record.get('event_id') or ''),
                'journal_event_type': str(payload.get('journal_event_type') or ''),
                'capture_event_type': str(payload.get('capture_event_type') or ''),
                'sale_origin': str(payload.get('sale_origin') or ''),
                'journal_capture_source': str(payload.get('journal_capture_source') or ''),
                'organization_id': payload.get('organization_id'),
                'location_id': payload.get('location_id'),
                'payment_status': str(payload.get('payment_status') or ''),
                'payment_reference': str(payload.get('payment_reference') or ''),
                'sale_total': str(payload.get('sale_total') or ''),
                'display_name': str(payload.get('display_name') or ''),
                'failure_reason': str(payload.get('failure_reason') or ''),
                'client_transaction_id': str(record.get('client_transaction_id') or ''),
                'queue_session_id': str(record.get('queue_session_id') or ''),
                'session_seq_no': record.get('session_seq_no'),
                'created_at': created_at or record.get('created_at'),
            }
        )
    return events


def _build_rotation_status(
    *,
    segment_path: Path,
    snapshot: dict,
    recovery,
    segment_max_bytes: int,
) -> dict:
    segment_size_bytes = segment_path.stat().st_size if segment_path.exists() else 0
    seal_pending = bool(snapshot.get('seal_pending'))
    sealed = bool(snapshot.get('sealed'))
    rotation_needed = False
    reason = 'El segmento aun esta por debajo del umbral de rotacion.'

    if sealed:
        reason = 'El segmento actual ya esta sellado.'
    elif seal_pending:
        rotation_needed = True
        reason = 'El sidecar marca footer pendiente; el segmento debe sellarse.'
    elif segment_size_bytes >= segment_max_bytes:
        rotation_needed = True
        reason = 'El segmento alcanzo el tamano maximo configurado y debe rotar.'

    action_allowed = rotation_needed and not recovery.truncated_tail and not recovery.corrupted_tail
    return {
        'segment_size_bytes': segment_size_bytes,
        'segment_max_bytes': segment_max_bytes,
        'seal_pending': seal_pending,
        'rotation_needed': rotation_needed,
        'action_allowed': action_allowed,
        'reason': reason,
    }


def _build_sealed_segment_history(
    *,
    root_dir: Path,
    stream_name: str,
    active_segment_id: str,
    limit: int,
) -> list[dict]:
    history: list[dict] = []
    snapshot_paths = sorted(
        root_dir.glob(f'{stream_name}-*.snapshot.json'),
        key=lambda item: item.name,
        reverse=True,
    )

    for snapshot_path in snapshot_paths:
        snapshot = load_snapshot_payload(snapshot_path)
        segment_id = str(snapshot.get('segment_id') or snapshot_path.name.replace('.snapshot.json', ''))
        if not segment_id or segment_id == active_segment_id:
            continue
        if not snapshot.get('sealed'):
            continue

        segment_path = root_dir / f'{segment_id}.jsonl'
        recovery = recover_segment_prefix(segment_path)
        last_record = recovery.records[-1] if recovery.records else {}
        last_created_at = parse_datetime(str(last_record.get('created_at') or ''))
        summary = snapshot.get('summary') or {}

        if recovery.truncated_tail or recovery.corrupted_tail:
            status = 'integrity_error'
            detail = recovery.error_message or 'El segmento sellado tiene una cola invalida.'
        elif recovery.footer:
            status = 'sealed'
            detail = 'Footer presente y validado.'
        else:
            status = 'footer_missing'
            detail = 'Snapshot sellado, pero el footer no esta presente en el segmento.'

        history.append(
            {
                'segment_id': segment_id,
                'segment_path': str(segment_path),
                'snapshot_path': str(snapshot_path),
                'record_count': int(snapshot.get('record_count') or recovery.record_count),
                'summary_total_sales': int(summary.get('total_sales') or 0),
                'summary_amount_total': str(summary.get('amount_total') or '0.00'),
                'footer_present': bool(recovery.footer),
                'status': status,
                'detail': detail,
                'last_event_id': str(snapshot.get('last_event_id') or recovery.last_event_id or ''),
                'last_event_created_at': last_created_at or last_record.get('created_at'),
                'segment_crc32': str(
                    (recovery.footer or {}).get('segment_crc32')
                    or snapshot.get('rolling_crc32')
                    or '00000000'
                ),
            }
        )
        if len(history) >= limit:
            break

    return history


def build_offline_limbo_payload() -> dict:
    context = build_offline_limbo_context()
    return {
        **context,
        'recent_events': [
            {
                **event,
                'created_at': event['created_at'].isoformat() if hasattr(event.get('created_at'), 'isoformat') else event.get('created_at'),
            }
            for event in context.get('recent_events', [])
        ],
        'sealed_segments': [
            {
                **segment,
                'last_event_created_at': (
                    segment['last_event_created_at'].isoformat()
                    if hasattr(segment.get('last_event_created_at'), 'isoformat')
                    else segment.get('last_event_created_at')
                ),
            }
            for segment in context.get('sealed_segments', [])
        ],
        'refreshed_at': timezone.now().isoformat(),
    }


def build_offline_segment_detail_payload(segment_id: str) -> dict:
    enabled = bool(getattr(settings, 'OFFLINE_JOURNAL_ENABLED', False))
    if not enabled:
        raise ValueError('runtime offline desactivado')

    root_value = str(getattr(settings, 'OFFLINE_JOURNAL_ROOT', '') or '').strip()
    if not root_value:
        raise ValueError('OFFLINE_JOURNAL_ROOT no esta configurado')

    stream_name = getattr(settings, 'OFFLINE_JOURNAL_STREAM_NAME', 'sales')
    normalized_segment_id = _validate_segment_id(segment_id=segment_id, stream_name=stream_name)
    root_dir = Path(root_value)
    if not root_dir.exists() or not root_dir.is_dir():
        raise ValueError(f'root offline invalido: {root_dir}')

    segment_path = root_dir / f'{normalized_segment_id}.jsonl'
    snapshot_path = root_dir / f'{normalized_segment_id}.snapshot.json'
    if not segment_path.exists():
        raise ValueError(f'segmento inexistente: {normalized_segment_id}')

    snapshot = load_snapshot_payload(snapshot_path)
    recovery = recover_segment_prefix(segment_path)
    recent_limit = max(1, int(getattr(settings, 'OFFLINE_JOURNAL_LIMBO_RECENT_LIMIT', 50)))
    summary = snapshot.get('summary') or rebuild_limbo_summary_from_records(
        recovery.records,
        limbo_recent_limit=recent_limit,
    )

    if recovery.truncated_tail or recovery.corrupted_tail:
        status = 'integrity_error'
        detail = recovery.error_message or 'El segmento tiene una cola invalida.'
    elif recovery.footer:
        status = 'sealed'
        detail = 'Footer presente y validado.'
    elif snapshot.get('sealed'):
        status = 'footer_missing'
        detail = 'Snapshot sellado, pero el footer no esta presente en el segmento.'
    else:
        status = 'open'
        detail = 'El segmento aun no esta sellado.'

    return {
        'segment_id': normalized_segment_id,
        'segment_path': str(segment_path),
        'snapshot_path': str(snapshot_path),
        'status': status,
        'detail': detail,
        'record_count': recovery.record_count,
        'last_valid_offset': recovery.last_valid_offset,
        'last_event_id': recovery.last_event_id,
        'last_record_hash': recovery.last_record_hash,
        'rolling_crc32': recovery.rolling_crc32,
        'footer_present': bool(recovery.footer),
        'footer': recovery.footer or {},
        'snapshot': {
            'record_count': int(snapshot.get('record_count') or 0),
            'last_offset_confirmed': int(snapshot.get('last_offset_confirmed') or 0),
            'last_event_id': str(snapshot.get('last_event_id') or ''),
            'last_record_hash': str(snapshot.get('last_record_hash') or ''),
            'rolling_crc32': str(snapshot.get('rolling_crc32') or '00000000'),
            'sealed': bool(snapshot.get('sealed')),
            'seal_pending': bool(snapshot.get('seal_pending')),
        },
        'ops_metadata': {
            'operational_review': dict((snapshot.get('ops_metadata') or {}).get('operational_review') or {}),
            'last_footer_revalidation': dict((snapshot.get('ops_metadata') or {}).get('last_footer_revalidation') or {}),
        },
        'summary': summary,
        'segment_health': {
            'truncated_tail': recovery.truncated_tail,
            'corrupted_tail': recovery.corrupted_tail,
            'error_message': recovery.error_message,
        },
        'recent_events': [
            {
                **event,
                'created_at': (
                    event['created_at'].isoformat()
                    if hasattr(event.get('created_at'), 'isoformat')
                    else event.get('created_at')
                ),
            }
            for event in _build_recent_offline_events(recovery.records, limit=min(10, recent_limit))
        ],
        'refreshed_at': timezone.now().isoformat(),
    }


def _validate_segment_id(*, segment_id: str, stream_name: str) -> str:
    normalized = str(segment_id or '').strip()
    if not normalized:
        raise ValueError('segment_id requerido')
    if any(separator in normalized for separator in ('/', '\\', '..')):
        raise ValueError('segment_id invalido')
    if not normalized.startswith(f'{stream_name}-'):
        raise ValueError('segment_id fuera del stream configurado')
    return normalized
