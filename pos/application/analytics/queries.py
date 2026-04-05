from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db.models import Avg, Case, Count, Exists, F, IntegerField, OuterRef, Sum, Value, When
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

OFFLINE_AUDIT_RESULT_OPTIONS = (
    ('', 'Todos'),
    ('footer_present', 'Footer OK'),
    ('footer_missing', 'Footer faltante'),
    ('review_marked', 'Revision registrada'),
)

OFFLINE_AUDIT_TIME_WINDOW_OPTIONS = (
    ('', 'Todo el periodo'),
    ('24h', 'Ultimas 24h'),
    ('72h', 'Ultimas 72h'),
    ('168h', 'Ultimos 7 dias'),
)

OFFLINE_AUDIT_FOOTER_PRESENCE_OPTIONS = (
    ('', 'Todos'),
    ('present', 'Footer presente'),
    ('missing', 'Footer ausente'),
)

OFFLINE_AUDIT_SORT_OPTIONS = (
    ('recent', 'Mas recientes'),
    ('footer_missing', 'Footer missing primero'),
    ('unreviewed', 'Sin revisar primero'),
)

OFFLINE_AUDIT_BULK_EVENT_TYPE_MAP = {
    'revalidate_footer': 'offline.segment_bulk_revalidated',
    'mark_operational_review': 'offline.segment_bulk_review_marked',
}


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


def _resolve_offline_audit_result(event_type: str, payload_json: dict) -> str:
    payload = dict(payload_json or {})
    stored_result = str(payload.get('audit_result') or '').strip()
    if stored_result:
        return stored_result
    if event_type == 'offline.segment_footer_revalidated':
        return 'footer_present' if payload.get('footer_present') else 'footer_missing'
    return 'review_marked'


def _resolve_offline_audit_result_label(audit_result: str) -> str:
    return {
        'footer_present': 'FOOTER_OK',
        'footer_missing': 'FOOTER_MISSING',
        'review_marked': 'REVIEW_MARKED',
    }.get(str(audit_result or '').strip(), 'UNKNOWN')


def _build_offline_audited_actions(
    desde,
    hasta,
    *,
    segment_id: str = '',
    time_window: str = '',
    action_type: str = '',
    organization_id: str = '',
    location_id: str = '',
    actor_id: str = '',
    segment_status: str = '',
    audit_result: str = '',
    footer_presence: str = '',
    sort_order: str = 'recent',
    critical_only: bool = False,
):
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
    )
    organization_options = list(
        base_queryset.exclude(organization__isnull=True)
        .values('organization_id', 'organization__name')
        .distinct()
        .order_by('organization__name')
    )
    location_options = list(
        base_queryset.exclude(location__isnull=True)
        .values('location_id', 'location__name')
        .distinct()
        .order_by('location__name')
    )
    actor_options = list(
        base_queryset.exclude(actor_user__isnull=True)
        .values('actor_user_id', 'actor_user__username')
        .distinct()
        .order_by('actor_user__username')
    )
    segment_status_options = []
    for value in base_queryset.values_list('payload_json__segment_status', flat=True).distinct():
        normalized_value = str(value or '').strip()
        if normalized_value:
            segment_status_options.append(normalized_value)
    segment_status_options.sort()

    queryset = base_queryset
    normalized_segment_id = str(segment_id or '').strip()
    if normalized_segment_id:
        queryset = queryset.filter(target_id__icontains=normalized_segment_id)

    normalized_time_window = str(time_window or '').strip()
    if normalized_time_window in {'24h', '72h', '168h'}:
        hours = int(normalized_time_window[:-1])
        queryset = queryset.filter(created_at__gte=timezone.now() - timedelta(hours=hours))
    else:
        normalized_time_window = ''

    normalized_action_type = str(action_type or '').strip()
    if normalized_action_type:
        queryset = queryset.filter(event_type=normalized_action_type)

    normalized_organization_id = str(organization_id or '').strip()
    if normalized_organization_id.isdigit():
        queryset = queryset.filter(organization_id=int(normalized_organization_id))
    else:
        normalized_organization_id = ''

    normalized_location_id = str(location_id or '').strip()
    if normalized_location_id.isdigit():
        queryset = queryset.filter(location_id=int(normalized_location_id))
    else:
        normalized_location_id = ''

    normalized_actor_id = str(actor_id or '').strip()
    if normalized_actor_id.isdigit():
        queryset = queryset.filter(actor_user_id=int(normalized_actor_id))
    else:
        normalized_actor_id = ''

    normalized_segment_status = str(segment_status or '').strip()
    if normalized_segment_status:
        queryset = queryset.filter(payload_json__segment_status=normalized_segment_status)

    normalized_footer_presence = str(footer_presence or '').strip()
    if normalized_footer_presence == 'present':
        queryset = queryset.filter(payload_json__footer_present=True)
    elif normalized_footer_presence == 'missing':
        queryset = queryset.exclude(payload_json__footer_present=True)
    else:
        normalized_footer_presence = ''

    normalized_audit_result = str(audit_result or '').strip()
    if normalized_audit_result == 'footer_present':
        queryset = queryset.filter(
            event_type='offline.segment_footer_revalidated',
            payload_json__footer_present=True,
        )
    elif normalized_audit_result == 'footer_missing':
        queryset = queryset.filter(
            event_type='offline.segment_footer_revalidated',
        ).exclude(payload_json__footer_present=True)
    elif normalized_audit_result == 'review_marked':
        queryset = queryset.filter(event_type='offline.segment_operational_review_marked')
    else:
        normalized_audit_result = ''

    normalized_sort_order = str(sort_order or '').strip()
    if normalized_sort_order not in {'recent', 'footer_missing', 'unreviewed'}:
        normalized_sort_order = 'recent'

    review_exists_queryset = AuditLog.objects.filter(
        target_model='OfflineJournalSegment',
        target_id=OuterRef('target_id'),
        event_type='offline.segment_operational_review_marked',
    )
    queryset = queryset.annotate(
        segment_has_review=Exists(review_exists_queryset),
        footer_missing_priority=Case(
            When(
                event_type='offline.segment_footer_revalidated',
                payload_json__footer_present=True,
                then=Value(1),
            ),
            When(
                event_type='offline.segment_operational_review_marked',
                then=Value(1),
            ),
            default=Value(0),
            output_field=IntegerField(),
        ),
        critical_priority=Case(
            When(
                event_type='offline.segment_footer_revalidated',
                payload_json__segment_status='sealed',
                payload_json__footer_present=True,
                then=Value(0),
            ),
            When(
                event_type='offline.segment_operational_review_marked',
                payload_json__segment_status='sealed',
                then=Value(0),
            ),
            default=Value(1),
            output_field=IntegerField(),
        ),
    )
    if critical_only:
        queryset = queryset.filter(critical_priority=1)
    if normalized_sort_order == 'footer_missing':
        queryset = queryset.order_by('footer_missing_priority', '-created_at', '-id')
    elif normalized_sort_order == 'unreviewed':
        queryset = queryset.order_by('segment_has_review', '-created_at', '-id')
    else:
        queryset = queryset.order_by('-created_at', '-id')

    items = [_decorate_offline_audit_item(item) for item in queryset[:10]]

    return {
        'queryset': queryset,
        'items': items,
        'selected_segment_id': normalized_segment_id,
        'selected_time_window': normalized_time_window,
        'selected_action_type': normalized_action_type,
        'selected_organization_id': normalized_organization_id,
        'selected_location_id': normalized_location_id,
        'selected_actor_id': normalized_actor_id,
        'selected_segment_status': normalized_segment_status,
        'selected_audit_result': normalized_audit_result,
        'selected_footer_presence': normalized_footer_presence,
        'selected_sort_order': normalized_sort_order,
        'time_window_options': OFFLINE_AUDIT_TIME_WINDOW_OPTIONS,
        'action_type_options': OFFLINE_AUDIT_EVENT_TYPE_OPTIONS,
        'organization_options': organization_options,
        'location_options': location_options,
        'actor_options': actor_options,
        'segment_status_options': segment_status_options,
        'audit_result_options': OFFLINE_AUDIT_RESULT_OPTIONS,
        'footer_presence_options': OFFLINE_AUDIT_FOOTER_PRESENCE_OPTIONS,
        'sort_options': OFFLINE_AUDIT_SORT_OPTIONS,
        'critical_only': bool(critical_only),
    }


def _build_offline_audit_context_fields(offline_audit_bundle: dict) -> dict:
    return {
        'offline_audited_actions': offline_audit_bundle['items'],
        'offline_audited_actions_count': offline_audit_bundle['queryset'].count(),
        'offline_audited_action_filter_segment_id': offline_audit_bundle['selected_segment_id'],
        'offline_audited_action_filter_time_window': offline_audit_bundle['selected_time_window'],
        'offline_audited_action_filter_type': offline_audit_bundle['selected_action_type'],
        'offline_audited_action_filter_organization': offline_audit_bundle['selected_organization_id'],
        'offline_audited_action_filter_location': offline_audit_bundle['selected_location_id'],
        'offline_audited_action_filter_actor': offline_audit_bundle['selected_actor_id'],
        'offline_audited_action_filter_segment_status': offline_audit_bundle['selected_segment_status'],
        'offline_audited_action_filter_result': offline_audit_bundle['selected_audit_result'],
        'offline_audited_action_filter_footer_presence': offline_audit_bundle['selected_footer_presence'],
        'offline_audited_action_filter_sort': offline_audit_bundle['selected_sort_order'],
        'offline_audited_action_time_window_options': offline_audit_bundle['time_window_options'],
        'offline_audited_action_type_options': offline_audit_bundle['action_type_options'],
        'offline_audited_action_organization_options': offline_audit_bundle['organization_options'],
        'offline_audited_action_location_options': offline_audit_bundle['location_options'],
        'offline_audited_action_actor_options': offline_audit_bundle['actor_options'],
        'offline_audited_action_segment_status_options': offline_audit_bundle['segment_status_options'],
        'offline_audited_action_result_options': offline_audit_bundle['audit_result_options'],
        'offline_audited_action_footer_presence_options': offline_audit_bundle['footer_presence_options'],
        'offline_audited_action_sort_options': offline_audit_bundle['sort_options'],
        'offline_audited_actions_critical_only': offline_audit_bundle['critical_only'],
    }


def _decorate_offline_audit_item(item):
    item.offline_segment_status = str((item.payload_json or {}).get('segment_status') or '').strip()
    item.offline_audit_result = _resolve_offline_audit_result(item.event_type, item.payload_json)
    item.offline_audit_result_label = _resolve_offline_audit_result_label(item.offline_audit_result)
    item.offline_is_critical = bool(getattr(item, 'critical_priority', 0))
    return item


def _serialize_offline_audit_item(item) -> dict:
    payload = dict(item.payload_json or {})
    return {
        'audit_log_id': item.id,
        'created_at': timezone.localtime(item.created_at).isoformat(),
        'event_type': item.event_type,
        'segment_id': str(item.target_id or ''),
        'target_model': str(item.target_model or ''),
        'segment_status': item.offline_segment_status,
        'audit_result': item.offline_audit_result,
        'audit_result_label': item.offline_audit_result_label,
        'footer_present': bool(payload.get('footer_present')),
        'organization_id': item.organization_id,
        'organization_name': item.organization.name if item.organization_id and item.organization else '',
        'location_id': item.location_id,
        'location_name': item.location.name if item.location_id and item.location else '',
        'actor_user_id': item.actor_user_id,
        'actor_username': item.actor_user.username if item.actor_user_id and item.actor_user else '',
        'critical': bool(item.offline_is_critical),
        'segment_has_review': bool(getattr(item, 'segment_has_review', False)),
    }


def build_analytics_dashboard_context(
    periodo: str = 'semana',
    desde_param=None,
    hasta_param=None,
    offline_action_segment_id: str = '',
    offline_action_time_window: str = '',
    offline_action_type: str = '',
    offline_action_organization: str = '',
    offline_action_location: str = '',
    offline_action_actor: str = '',
    offline_action_segment_status: str = '',
    offline_action_result: str = '',
    offline_action_footer_presence: str = '',
    offline_action_sort: str = 'recent',
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
        segment_id=offline_action_segment_id,
        time_window=offline_action_time_window,
        action_type=offline_action_type,
        organization_id=offline_action_organization,
        location_id=offline_action_location,
        actor_id=offline_action_actor,
        segment_status=offline_action_segment_status,
        audit_result=offline_action_result,
        footer_presence=offline_action_footer_presence,
        sort_order=offline_action_sort,
    )

    context = {
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
    }
    context.update(_build_offline_audit_context_fields(offline_audit_bundle))
    return context


def build_offline_critical_incidents_context(
    periodo: str = 'semana',
    desde_param=None,
    hasta_param=None,
    offline_action_segment_id: str = '',
    offline_action_time_window: str = '',
    offline_action_type: str = '',
    offline_action_organization: str = '',
    offline_action_location: str = '',
    offline_action_actor: str = '',
    offline_action_segment_status: str = '',
    offline_action_result: str = '',
    offline_action_footer_presence: str = '',
    offline_action_sort: str = 'footer_missing',
):
    hoy = timezone.localdate()
    desde, hasta = _resolve_period(periodo, hoy, desde_param, hasta_param)
    offline_audit_bundle = _build_offline_audited_actions(
        desde,
        hasta,
        segment_id=offline_action_segment_id,
        time_window=offline_action_time_window,
        action_type=offline_action_type,
        organization_id=offline_action_organization,
        location_id=offline_action_location,
        actor_id=offline_action_actor,
        segment_status=offline_action_segment_status,
        audit_result=offline_action_result,
        footer_presence=offline_action_footer_presence,
        sort_order=offline_action_sort,
        critical_only=True,
    )
    offline_bulk_metrics = _build_offline_bulk_action_metrics(
        desde,
        hasta,
        time_window=offline_action_time_window,
        organization_id=offline_action_organization,
        location_id=offline_action_location,
        actor_id=offline_action_actor,
    )
    context = {
        'periodo': periodo,
        'desde': desde,
        'hasta': hasta,
        'periods': [('hoy', 'Hoy'), ('semana', '7 dias'), ('mes', '30 dias')],
        'offline_critical_incidents_count': offline_audit_bundle['queryset'].count(),
        'offline_bulk_metrics': offline_bulk_metrics,
    }
    context.update(_build_offline_audit_context_fields(offline_audit_bundle))
    return context


def build_offline_critical_incidents_export_payload(
    periodo: str = 'semana',
    desde_param=None,
    hasta_param=None,
    offline_action_segment_id: str = '',
    offline_action_time_window: str = '',
    offline_action_type: str = '',
    offline_action_organization: str = '',
    offline_action_location: str = '',
    offline_action_actor: str = '',
    offline_action_segment_status: str = '',
    offline_action_result: str = '',
    offline_action_footer_presence: str = '',
    offline_action_sort: str = 'footer_missing',
) -> dict:
    hoy = timezone.localdate()
    desde, hasta = _resolve_period(periodo, hoy, desde_param, hasta_param)
    offline_audit_bundle = _build_offline_audited_actions(
        desde,
        hasta,
        segment_id=offline_action_segment_id,
        time_window=offline_action_time_window,
        action_type=offline_action_type,
        organization_id=offline_action_organization,
        location_id=offline_action_location,
        actor_id=offline_action_actor,
        segment_status=offline_action_segment_status,
        audit_result=offline_action_result,
        footer_presence=offline_action_footer_presence,
        sort_order=offline_action_sort,
        critical_only=True,
    )
    items = [_decorate_offline_audit_item(item) for item in offline_audit_bundle['queryset']]
    return {
        'periodo': periodo,
        'desde': str(desde),
        'hasta': str(hasta),
        'critical_only': True,
        'sort_order': offline_audit_bundle['selected_sort_order'],
        'count': len(items),
        'items': [_serialize_offline_audit_item(item) for item in items],
    }


def _build_offline_bulk_action_metrics(
    desde,
    hasta,
    *,
    time_window: str = '',
    organization_id: str = '',
    location_id: str = '',
    actor_id: str = '',
):
    queryset = AuditLog.objects.filter(
        event_type__in=list(OFFLINE_AUDIT_BULK_EVENT_TYPE_MAP.values()),
        created_at__date__gte=desde,
        created_at__date__lte=hasta,
    ).select_related('organization', 'location', 'actor_user')

    normalized_time_window = str(time_window or '').strip()
    if normalized_time_window in {'24h', '72h', '168h'}:
        hours = int(normalized_time_window[:-1])
        queryset = queryset.filter(created_at__gte=timezone.now() - timedelta(hours=hours))

    normalized_organization_id = str(organization_id or '').strip()
    if normalized_organization_id.isdigit():
        queryset = queryset.filter(organization_id=int(normalized_organization_id))

    normalized_location_id = str(location_id or '').strip()
    if normalized_location_id.isdigit():
        queryset = queryset.filter(location_id=int(normalized_location_id))

    normalized_actor_id = str(actor_id or '').strip()
    if normalized_actor_id.isdigit():
        queryset = queryset.filter(actor_user_id=int(normalized_actor_id))

    items = list(queryset.order_by('-created_at'))
    processed_total = 0
    succeeded_total = 0
    failed_total = 0
    revalidate_runs = 0
    review_runs = 0
    last_run = items[0] if items else None

    for item in items:
        payload = dict(item.payload_json or {})
        processed_total += int(payload.get('processed') or 0)
        succeeded_total += int(payload.get('succeeded') or 0)
        failed_total += int(payload.get('failed') or 0)
        if item.event_type == OFFLINE_AUDIT_BULK_EVENT_TYPE_MAP['revalidate_footer']:
            revalidate_runs += 1
        elif item.event_type == OFFLINE_AUDIT_BULK_EVENT_TYPE_MAP['mark_operational_review']:
            review_runs += 1

    return {
        'runs_count': len(items),
        'processed_total': processed_total,
        'succeeded_total': succeeded_total,
        'failed_total': failed_total,
        'revalidate_runs': revalidate_runs,
        'review_runs': review_runs,
        'last_run_at': timezone.localtime(last_run.created_at) if last_run else None,
        'last_run_actor': (
            last_run.actor_user.username
            if last_run and last_run.actor_user_id and last_run.actor_user
            else ''
        ),
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


def build_offline_limbo_payload(requested_segment_id: str = '') -> dict:
    context = build_offline_limbo_context()
    return {
        **context,
        'requested_segment_id': str(requested_segment_id or '').strip(),
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
