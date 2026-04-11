from __future__ import annotations

import csv
import json
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from pos.application.analytics import (
    OfflineLimboActionError,
    build_analytics_dashboard_context,
    build_offline_audited_actions_export_payload,
    build_offline_bulk_run_detail_payload,
    build_offline_bulk_runs_export_payload,
    build_offline_bulk_runs_context,
    build_offline_critical_incidents_context,
    build_offline_critical_incidents_export_payload,
    build_offline_limbo_context,
    build_offline_limbo_payload,
    build_offline_retention_actions_context,
    build_offline_retention_actions_export_payload,
    build_offline_retention_receipt_payload,
    build_offline_segment_detail_payload,
    execute_offline_limbo_action,
    execute_offline_segment_bulk_action,
    execute_offline_segment_action,
)
from pos.application.sales import (
    PosSaleError,
    resolve_accounting_adjustment,
    resolve_payment_exception,
    resolve_post_close_replay_alert,
)
from pos.models import AuditLog


OFFLINE_RETENTION_EVENT_TYPES = (
    'offline.segment_usb_exported',
    'offline.segment_purged_after_usb',
)
OFFLINE_RETENTION_EVENT_LABELS = {
    'offline.segment_usb_exported': 'USB_EXPORTED',
    'offline.segment_purged_after_usb': 'PURGED_AFTER_USB',
}


def _build_offline_batch_retention_hint(audit: AuditLog | None) -> dict:
    if not audit:
        return {
            'applicable': False,
            'receipt_type': '',
            'event_label': '',
        }
    payload = dict(audit.payload_json or {})
    return {
        'applicable': True,
        'receipt_type': str(payload.get('receipt_type') or 'N/A'),
        'event_label': OFFLINE_RETENTION_EVENT_LABELS.get(audit.event_type, audit.event_type),
    }


def dashboard_analytics(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    context = build_analytics_dashboard_context(
        periodo=request.GET.get('periodo', 'semana'),
        desde_param=request.GET.get('desde'),
        hasta_param=request.GET.get('hasta'),
        offline_action_segment_id=request.GET.get('offline_action_segment_id', ''),
        offline_action_time_window=request.GET.get('offline_action_time_window', ''),
        offline_action_type=request.GET.get('offline_action_type', ''),
        offline_action_organization=request.GET.get('offline_action_organization', ''),
        offline_action_location=request.GET.get('offline_action_location', ''),
        offline_action_actor=request.GET.get('offline_action_actor', ''),
        offline_action_segment_status=request.GET.get('offline_action_segment_status', ''),
        offline_action_result=request.GET.get('offline_action_result', ''),
        offline_action_footer_presence=request.GET.get('offline_action_footer_presence', ''),
        offline_action_sort=request.GET.get('offline_action_sort', 'recent'),
    )
    context.update(
        _build_offline_actions_panel_context(
            route_name='dashboard_analytics',
            periodo=context['periodo'],
            desde=context['desde'],
            hasta=context['hasta'],
            title='ACCIONES OFFLINE AUDITADAS',
            subtitle='Revision centralizada del journal offline dentro del periodo activo.',
            force_render=False,
            critical_view=False,
            export_query_params=_build_offline_actions_query_params_from_context(context),
            tertiary_href=f"{reverse('dashboard_offline_retention')}?{urlencode(_build_period_query_params(context['periodo'], context['desde'], context['hasta']))}",
            tertiary_label='Retencion',
        )
    )
    return render(request, 'pos/dashboard.html', context)


def dashboard_offline_incidents(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    context = build_offline_critical_incidents_context(
        periodo=request.GET.get('periodo', 'semana'),
        desde_param=request.GET.get('desde'),
        hasta_param=request.GET.get('hasta'),
        offline_action_segment_id=request.GET.get('offline_action_segment_id', ''),
        offline_action_time_window=request.GET.get('offline_action_time_window', ''),
        offline_action_type=request.GET.get('offline_action_type', ''),
        offline_action_organization=request.GET.get('offline_action_organization', ''),
        offline_action_location=request.GET.get('offline_action_location', ''),
        offline_action_actor=request.GET.get('offline_action_actor', ''),
        offline_action_segment_status=request.GET.get('offline_action_segment_status', ''),
        offline_action_result=request.GET.get('offline_action_result', ''),
        offline_action_footer_presence=request.GET.get('offline_action_footer_presence', ''),
        offline_action_sort=request.GET.get('offline_action_sort', 'footer_missing'),
    )
    context.update(
        _build_offline_actions_panel_context(
            route_name='dashboard_offline_incidents',
            periodo=context['periodo'],
            desde=context['desde'],
            hasta=context['hasta'],
            title='INCIDENTES OFFLINE CRITICOS',
            subtitle='Solo segmentos con footer faltante o estado distinto de sealed.',
            force_render=True,
            critical_view=True,
            export_query_params=_build_offline_actions_query_params_from_context(context),
        )
    )
    context['offline_bulk_runs_href'] = (
        f"{reverse('dashboard_offline_incident_batches')}?"
        f"{urlencode(_build_offline_bulk_query_params_from_context(context))}"
    )
    return render(request, 'pos/offline_incidents.html', context)


def dashboard_offline_retention(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    context = build_offline_retention_actions_context(
        periodo=request.GET.get('periodo', 'semana'),
        desde_param=request.GET.get('desde'),
        hasta_param=request.GET.get('hasta'),
        offline_action_segment_id=request.GET.get('offline_action_segment_id', ''),
        offline_action_time_window=request.GET.get('offline_action_time_window', ''),
        offline_action_type=request.GET.get('offline_action_type', ''),
        offline_action_organization=request.GET.get('offline_action_organization', ''),
        offline_action_location=request.GET.get('offline_action_location', ''),
        offline_action_actor=request.GET.get('offline_action_actor', ''),
        offline_action_segment_status=request.GET.get('offline_action_segment_status', ''),
        offline_action_result=request.GET.get('offline_action_result', ''),
        offline_action_sort=request.GET.get('offline_action_sort', 'recent'),
    )
    context.update(
        _build_offline_actions_panel_context(
            route_name='dashboard_offline_retention',
            periodo=context['periodo'],
            desde=context['desde'],
            hasta=context['hasta'],
            title='RETENCION OFFLINE',
            subtitle='Exportaciones USB y purges manuales del journal offline dentro del periodo activo.',
            force_render=True,
            critical_view=False,
            export_query_params=_build_offline_actions_query_params_from_context(context),
            secondary_route_name='dashboard_analytics',
            secondary_label='Volver a analytics',
        )
    )
    context['offline_retention_incidents_href'] = (
        f"{reverse('dashboard_offline_incidents')}?"
        f"{urlencode(_build_period_query_params(context['periodo'], context['desde'], context['hasta']))}"
    )
    context['offline_retention_full_href'] = (
        f"{reverse('dashboard_offline_retention')}?"
        f"{urlencode(_build_period_query_params(context['periodo'], context['desde'], context['hasta']))}"
    )
    context['offline_retention_show_period_tabs'] = not context.get(
        'offline_audited_actions_single_segment_drilldown', False
    )
    focus_segment_id = str(context.get('offline_audited_actions_drilldown_segment_id') or '').strip()
    context['offline_retention_focus_segment_id'] = focus_segment_id
    context['offline_retention_focus_segment_href'] = (
        f'{reverse("dashboard_offline_limbo_segment_detail")}?{urlencode({"segment_id": focus_segment_id})}'
        if focus_segment_id
        else ''
    )
    focus_summary = None
    if context.get('offline_audited_actions_single_segment_drilldown') and context.get('offline_audited_actions'):
        focus_action = context['offline_audited_actions'][0]
        retention_summary = getattr(focus_action, 'offline_retention_summary', {}) or {}
        focus_summary = {
            'event_label': getattr(focus_action, 'offline_event_type_label', 'OFFLINE_EVENT'),
            'receipt_type': retention_summary.get('receipt_type') or 'N/A',
            'detail_label': retention_summary.get('detail_label') or 'N/A',
            'detail_value': retention_summary.get('detail_value') or 'N/A',
            'meta_value': retention_summary.get('meta_value') or 'N/A',
            'reason': retention_summary.get('reason') or 'N/A',
        }
    context['offline_retention_focus_summary'] = focus_summary
    return render(request, 'pos/offline_retention.html', context)


def dashboard_offline_incident_batches(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    context = build_offline_bulk_runs_context(
        periodo=request.GET.get('periodo', 'semana'),
        desde_param=request.GET.get('desde'),
        hasta_param=request.GET.get('hasta'),
        offline_action_time_window=request.GET.get('offline_action_time_window', ''),
        offline_action_organization=request.GET.get('offline_action_organization', ''),
        offline_action_location=request.GET.get('offline_action_location', ''),
        offline_action_actor=request.GET.get('offline_action_actor', ''),
        offline_bulk_action_type=request.GET.get('offline_bulk_action_type', ''),
        offline_bulk_audit_log=request.GET.get('offline_bulk_audit_log', ''),
        offline_bulk_batch_id=request.GET.get('offline_bulk_batch_id', ''),
        offline_bulk_correlation_id=request.GET.get('offline_bulk_correlation_id', ''),
    )
    context['offline_bulk_clear_href'] = (
        f"{reverse('dashboard_offline_incident_batches')}?"
        f"{urlencode(_build_period_query_params(context['periodo'], context['desde'], context['hasta']))}"
    )
    context['offline_bulk_back_to_incidents_href'] = (
        f"{reverse('dashboard_offline_incidents')}?"
        f"{urlencode(_build_offline_bulk_back_to_incidents_query_params(context))}"
    )
    export_query = urlencode(_build_offline_bulk_export_query_params_from_context(context))
    context['offline_bulk_export_json_href'] = (
        f"{reverse('dashboard_offline_incident_batches_export_json')}?{export_query}"
    )
    context['offline_bulk_export_csv_href'] = (
        f"{reverse('dashboard_offline_incident_batches_export_csv')}?{export_query}"
    )
    context['offline_bulk_selected_run_json_href'] = (
        _build_offline_batch_json_href(context['offline_bulk_selected_run'].id)
        if context.get('offline_bulk_selected_run')
        else ''
    )
    context['offline_bulk_selected_run_html_href'] = (
        _build_offline_batch_html_href(
            context['offline_bulk_selected_run'].id,
            batch_id=context['offline_bulk_selected_run'].target_id,
            correlation_id=context['offline_bulk_selected_run'].correlation_id,
        )
        if context.get('offline_bulk_selected_run')
        else ''
    )
    context['offline_bulk_selected_run_retention_receipt_json_href'] = (
        _build_offline_retention_receipt_json_href(
            context['offline_bulk_selected_run'].bulk_retention_receipt_audit_log_id
        )
        if (
            context.get('offline_bulk_selected_run')
            and getattr(context['offline_bulk_selected_run'], 'bulk_retention_receipt_audit_log_id', 0)
        )
        else ''
    )
    return render(request, 'pos/offline_incident_batches.html', context)


def dashboard_offline_incident_batches_export_json(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect
    return JsonResponse(_build_offline_bulk_runs_export_payload_from_request(request))


def dashboard_offline_incident_batches_export_csv(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    payload = _build_offline_bulk_runs_export_payload_from_request(request)
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            'audit_log_id',
            'created_at',
            'event_type',
            'action_label',
            'batch_id',
            'processed',
            'succeeded',
            'failed',
            'organization_name',
            'location_name',
            'actor_username',
            'selected',
            'retention_hint',
            'retention_receipt_json_url',
            'detail_json_url',
            'detail_html_url',
            'auditlog_url',
        ]
    )
    for item in payload['items']:
        writer.writerow(
            [
                item['audit_log_id'],
                item['created_at'],
                item['event_type'],
                item['action_label'],
                item['batch_id'],
                item['processed'],
                item['succeeded'],
                item['failed'],
                item['organization_name'],
                item['location_name'],
                item['actor_username'],
                'YES' if item['selected'] else 'NO',
                item.get('retention_hint', ''),
                item.get('retention_receipt_json_url', ''),
                item['detail_json_url'],
                item.get('detail_html_url', ''),
                item.get('auditlog_url', ''),
            ]
        )
    response = HttpResponse(buffer.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename=\"offline-batch-runs.csv\"'
    return response


def dashboard_offline_incident_batch_json(request):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    try:
        payload = _enrich_offline_bulk_run_detail_urls(
            build_offline_bulk_run_detail_payload(
                audit_log_id=request.GET.get('audit_log_id', ''),
                batch_id=request.GET.get('batch_id', ''),
                correlation_id=request.GET.get('correlation_id', ''),
            )
        )
        return JsonResponse(payload)
    except ValueError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)


def dashboard_offline_incident_batch_detail(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect
    try:
        detail = _enrich_offline_bulk_run_detail_urls(
            build_offline_bulk_run_detail_payload(
                audit_log_id=request.GET.get('audit_log_id', ''),
                batch_id=request.GET.get('batch_id', ''),
                correlation_id=request.GET.get('correlation_id', ''),
            )
        )
    except ValueError:
        return redirect('dashboard_offline_incident_batches')

    batch_detail_segments = _build_offline_batch_segment_references(detail)
    batch_detail_segments = _enrich_offline_batch_segments_with_live_state(batch_detail_segments)
    context = {
        'batch_detail': detail,
        'batch_detail_segments': batch_detail_segments,
        'batch_detail_segment_summary': _build_offline_batch_segment_live_summary(batch_detail_segments),
        'batch_detail_json_href': detail['detail_json_url'],
        'batch_detail_html_href': detail['detail_html_url'],
        'batch_detail_auditlog_href': detail['auditlog_url'],
        'batch_detail_retention_receipt_json_href': detail['retention_receipt_json_url'],
        'batch_detail_payload_pretty': json.dumps(detail['payload_json'], ensure_ascii=False, indent=2, sort_keys=True),
        'batch_detail_back_href': _build_offline_batch_back_href(request),
    }
    return render(request, 'pos/offline_incident_batch_detail.html', context)


def _enrich_offline_bulk_run_detail_urls(detail: dict) -> dict:
    enriched = dict(detail or {})
    if not enriched:
        return enriched
    enriched['detail_json_url'] = _build_offline_batch_json_href(
        enriched.get('audit_log_id', ''),
        batch_id=enriched.get('batch_id', ''),
        correlation_id=enriched.get('correlation_id', ''),
    )
    enriched['detail_html_url'] = _build_offline_batch_html_href(
        enriched.get('audit_log_id', ''),
        batch_id=enriched.get('batch_id', ''),
        correlation_id=enriched.get('correlation_id', ''),
    )
    enriched['auditlog_url'] = _build_auditlog_admin_href(enriched['audit_log_id'])
    enriched['retention_receipt_json_url'] = (
        _build_offline_retention_receipt_json_href(enriched['retention_receipt_audit_log_id'])
        if enriched.get('retention_receipt_audit_log_id')
        else ''
    )
    return enriched


def _enrich_offline_segment_detail_urls(detail: dict) -> dict:
    enriched = dict(detail or {})
    if not enriched:
        return enriched
    enriched['detail_json_url'] = _build_offline_segment_json_href(enriched.get('segment_id', ''))
    enriched['detail_html_url'] = _build_offline_segment_html_href(enriched.get('segment_id', ''))
    enriched['auditlog_url'] = (
        _build_auditlog_admin_href(enriched['latest_audit_log_id'])
        if enriched.get('latest_audit_log_id')
        else ''
    )
    return enriched


def _enrich_offline_limbo_payload_auditlog_urls(payload: dict) -> dict:
    enriched = dict(payload or {})
    limbo = dict(enriched.get('limbo') or {})
    limbo['auditlog_url'] = (
        _build_auditlog_admin_href(limbo['latest_audit_log_id'])
        if limbo.get('latest_audit_log_id')
        else ''
    )
    enriched['limbo'] = limbo
    enriched['sealed_segments'] = [
        {
            **dict(item or {}),
            'auditlog_url': (
                _build_auditlog_admin_href((item or {}).get('latest_audit_log_id'))
                if (item or {}).get('latest_audit_log_id')
                else ''
            ),
        }
        for item in enriched.get('sealed_segments', [])
    ]
    return enriched


def _build_auditlog_admin_href(audit_log_id='') -> str:
    normalized_audit_log_id = int(audit_log_id or 0)
    return reverse('admin:pos_auditlog_change', args=[normalized_audit_log_id])


def dashboard_offline_incidents_export_json(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    return JsonResponse(_build_offline_critical_incidents_export_payload_from_request(request))


def dashboard_offline_audited_actions_export_json(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    return JsonResponse(_build_offline_audited_actions_export_payload_from_request(request))


def dashboard_offline_audited_actions_export_csv(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    payload = _build_offline_audited_actions_export_payload_from_request(request)
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            'audit_log_id',
            'created_at',
            'event_type',
            'segment_id',
            'segment_status',
            'audit_result',
            'footer_present',
            'organization_name',
            'location_name',
            'actor_username',
            'critical',
            'segment_has_review',
            'retention_hint',
            'receipt_json_url',
            'auditlog_url',
        ]
    )
    for item in payload['items']:
        writer.writerow(
            [
                item['audit_log_id'],
                item['created_at'],
                item['event_type'],
                item['segment_id'],
                item['segment_status'],
                item['audit_result'],
                'YES' if item['footer_present'] else 'NO',
                item['organization_name'],
                item['location_name'],
                item['actor_username'],
                'YES' if item['critical'] else 'NO',
                'YES' if item['segment_has_review'] else 'NO',
                ((item.get('retention_summary') or {}).get('hint') or ''),
                item.get('receipt_json_url', ''),
                item.get('auditlog_url', ''),
            ]
        )
    response = HttpResponse(buffer.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename=\"offline-audited-actions.csv\"'
    return response


def dashboard_offline_retention_export_json(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    return JsonResponse(_build_offline_retention_actions_export_payload_from_request(request))


def dashboard_offline_retention_export_csv(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    payload = _build_offline_retention_actions_export_payload_from_request(request)
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            'audit_log_id',
            'created_at',
            'event_type',
            'segment_id',
            'segment_status',
            'audit_result',
            'footer_present',
            'organization_name',
            'location_name',
            'actor_username',
            'critical',
            'segment_has_review',
            'retention_hint',
            'receipt_json_url',
            'auditlog_url',
        ]
    )
    for item in payload['items']:
        writer.writerow(
            [
                item['audit_log_id'],
                item['created_at'],
                item['event_type'],
                item['segment_id'],
                item['segment_status'],
                item['audit_result'],
                'YES' if item['footer_present'] else 'NO',
                item['organization_name'],
                item['location_name'],
                item['actor_username'],
                'YES' if item['critical'] else 'NO',
                'YES' if item['segment_has_review'] else 'NO',
                ((item.get('retention_summary') or {}).get('hint') or ''),
                item.get('receipt_json_url', ''),
                item.get('auditlog_url', ''),
            ]
        )
    response = HttpResponse(buffer.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename=\"offline-retention-actions.csv\"'
    return response


def dashboard_offline_retention_receipt_json(request):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    try:
        return JsonResponse(
            build_offline_retention_receipt_payload(
                audit_log_id=request.GET.get('audit_log_id', ''),
            )
        )
    except ValueError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)


def dashboard_offline_incidents_export_csv(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    payload = _build_offline_critical_incidents_export_payload_from_request(request)
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            'audit_log_id',
            'created_at',
            'event_type',
            'segment_id',
            'segment_status',
            'audit_result',
            'footer_present',
            'organization_name',
            'location_name',
            'actor_username',
            'critical',
            'segment_has_review',
            'auditlog_url',
        ]
    )
    for item in payload['items']:
        writer.writerow(
            [
                item['audit_log_id'],
                item['created_at'],
                item['event_type'],
                item['segment_id'],
                item['segment_status'],
                item['audit_result'],
                'YES' if item['footer_present'] else 'NO',
                item['organization_name'],
                item['location_name'],
                item['actor_username'],
                'YES' if item['critical'] else 'NO',
                'YES' if item['segment_has_review'] else 'NO',
                item.get('auditlog_url', ''),
            ]
        )
    response = HttpResponse(buffer.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename=\"offline-critical-incidents.csv\"'
    return response


def dashboard_offline_incidents_bulk_revalidate_json(request):
    return _execute_offline_segment_bulk_action_json(request, action='revalidate_footer')


def dashboard_offline_incidents_bulk_review_json(request):
    return _execute_offline_segment_bulk_action_json(request, action='mark_operational_review')


def dashboard_offline_limbo(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect

    context = _enrich_offline_limbo_payload_auditlog_urls(build_offline_limbo_context())
    context['initial_segment_id'] = str(request.GET.get('segment_id', '') or '').strip()
    return render(
        request,
        'pos/offline_limbo.html',
        context,
    )


def dashboard_offline_limbo_json(request):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    return JsonResponse(
        _enrich_offline_limbo_payload_auditlog_urls(
            build_offline_limbo_payload(request.GET.get('segment_id', ''))
        )
    )


def dashboard_offline_limbo_segment_json(request):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    segment_id = request.GET.get('segment_id', '')
    try:
        return JsonResponse(
            _enrich_offline_segment_detail_urls(
                build_offline_segment_detail_payload(segment_id)
            )
        )
    except ValueError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)


def dashboard_offline_limbo_segment_detail(request):
    access_redirect = _require_admin_dashboard_access(request)
    if access_redirect:
        return access_redirect
    try:
        detail = _enrich_offline_segment_detail_urls(
            build_offline_segment_detail_payload(request.GET.get('segment_id', ''))
        )
    except ValueError:
        return redirect('dashboard_offline_limbo')

    context = {
        'segment_detail': detail,
        'segment_detail_json_href': detail['detail_json_url'],
        'segment_detail_html_href': detail['detail_html_url'],
        'segment_detail_auditlog_href': detail['auditlog_url'],
        'segment_detail_back_href': _build_offline_segment_back_href(request, detail['segment_id']),
        'segment_detail_can_reconcile_sidecar': True,
        'segment_detail_can_reseal': (
            detail['status'] == 'footer_missing'
            or bool((detail.get('snapshot') or {}).get('seal_pending'))
        ),
        'segment_detail_payload_pretty': json.dumps(detail, ensure_ascii=False, indent=2, sort_keys=True),
    }
    return render(request, 'pos/offline_segment_detail.html', context)


def dashboard_offline_limbo_segment_revalidate_json(request):
    return _execute_offline_segment_action_json(request, action='revalidate_footer')


def dashboard_offline_limbo_segment_review_json(request):
    return _execute_offline_segment_action_json(request, action='mark_operational_review')


def dashboard_offline_limbo_segment_reconcile_json(request):
    return _execute_offline_segment_action_json(request, action='reconcile_sidecar')


def dashboard_offline_limbo_segment_reseal_json(request):
    return _execute_offline_segment_action_json(request, action='reseal_segment')


def dashboard_offline_limbo_segment_export_usb_json(request):
    return _execute_offline_segment_action_json(request, action='export_usb')


def dashboard_offline_limbo_segment_purge_after_usb_json(request):
    return _execute_offline_segment_action_json(request, action='purge_after_usb')


def dashboard_offline_limbo_reconcile_json(request):
    return _execute_offline_limbo_action_json(request, action='reconcile_sidecar')


def dashboard_offline_limbo_reseal_json(request):
    return _execute_offline_limbo_action_json(request, action='reseal_segment')


def dashboard_offline_limbo_seal_json(request):
    return _execute_offline_limbo_action_json(request, action='seal_active_segment')


def resolver_excepcion_pago(request):
    access_redirect = _require_admin_dashboard_access(request, allow_get_redirect=True)
    if access_redirect:
        return access_redirect

    if request.method != 'POST':
        return redirect('dashboard_analytics')

    try:
        resolve_payment_exception(
            audit_log_id=int(request.POST.get('audit_log_id', '0') or 0),
            user=request.user,
            resolution_note=request.POST.get('resolution_note', ''),
            resolution_action=request.POST.get('resolution_action', ''),
            resolution_reference=request.POST.get('resolution_reference', ''),
        )
        messages.success(request, 'La excepcion de pago fue resuelta correctamente.')
    except (ValueError, PosSaleError):
        messages.error(request, 'No se pudo resolver la excepcion de pago con la informacion enviada.')
        return redirect('dashboard_analytics')

    return redirect('dashboard_analytics')


def resolver_ajuste_contable(request):
    access_redirect = _require_admin_dashboard_access(request, allow_get_redirect=True)
    if access_redirect:
        return access_redirect

    if request.method != 'POST':
        return redirect('dashboard_analytics')

    try:
        resolve_accounting_adjustment(
            adjustment_id=int(request.POST.get('adjustment_id', '0') or 0),
            user=request.user,
            resolution_note=request.POST.get('resolution_note', ''),
            resolution_reference=request.POST.get('resolution_reference', ''),
            settlement_mode=request.POST.get('settlement_mode', ''),
        )
        messages.success(request, 'El ajuste contable fue marcado como resuelto.')
    except ValueError:
        messages.error(request, 'No se pudo resolver el ajuste contable con la informacion enviada.')
        return redirect('dashboard_analytics')
    except PosSaleError as exc:
        messages.error(request, exc.message)
        return redirect('dashboard_analytics')

    return redirect('dashboard_analytics')


def resolver_alerta_replay(request):
    access_redirect = _require_admin_dashboard_access(request, allow_get_redirect=True)
    if access_redirect:
        return access_redirect

    if request.method != 'POST':
        return redirect('dashboard_analytics')

    try:
        resolve_post_close_replay_alert(
            audit_log_id=int(request.POST.get('audit_log_id', '0') or 0),
            user=request.user,
            resolution_note=request.POST.get('resolution_note', ''),
        )
        messages.success(request, 'La alerta temporal de replay fue marcada como revisada.')
    except (ValueError, PosSaleError):
        messages.error(request, 'No se pudo resolver la alerta temporal con la informacion enviada.')
        return redirect('dashboard_analytics')

    return redirect('dashboard_analytics')


def _require_admin_dashboard_access(request, *, allow_get_redirect: bool = False):
    if not request.user.is_authenticated:
        return redirect('pos_login')
    if request.user.is_superuser:
        return None
    if hasattr(request.user, 'empleado') and request.user.empleado.rol == 'ADMIN':
        return None
    return redirect('pos_index' if allow_get_redirect or request.method == 'GET' else 'pos_index')


def _require_admin_dashboard_api_access(request):
    if not request.user.is_authenticated:
        return JsonResponse({'detail': 'auth required'}, status=401)
    if request.user.is_superuser:
        return None
    if hasattr(request.user, 'empleado') and request.user.empleado.rol == 'ADMIN':
        return None
    return JsonResponse({'detail': 'admin required'}, status=403)


def _execute_offline_limbo_action_json(request, *, action: str):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    if request.method != 'POST':
        return JsonResponse({'detail': 'method not allowed'}, status=405)
    try:
        return JsonResponse(execute_offline_limbo_action(action=action))
    except OfflineLimboActionError as exc:
        return JsonResponse(
            {
                'detail': str(exc),
                'action': {
                    'name': action,
                    'performed': False,
                },
            },
            status=409,
        )


def _execute_offline_segment_action_json(request, *, action: str):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    if request.method != 'POST':
        return JsonResponse({'detail': 'method not allowed'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'detail': 'json body invalido'}, status=400)
    segment_id = str(body.get('segment_id') or '').strip()
    try:
        return JsonResponse(
            execute_offline_segment_action(
                action=action,
                segment_id=segment_id,
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                usb_root=str(body.get('usb_root') or '').strip(),
                reason=str(body.get('reason') or '').strip(),
                manager_override=bool(body.get('manager_override')),
                usb_export_receipt_signature=str(body.get('usb_export_receipt_signature') or '').strip(),
            )
        )
    except OfflineLimboActionError as exc:
        return JsonResponse(
            {
                'detail': str(exc),
                'action': {
                    'name': action,
                    'performed': False,
                },
            },
            status=409,
        )


def _build_offline_actions_panel_context(
    *,
    route_name: str,
    periodo,
    desde,
    hasta,
    title: str,
    subtitle: str,
    force_render: bool,
    critical_view: bool,
    export_query_params=None,
    secondary_route_name: str | None = None,
    secondary_label: str | None = None,
    tertiary_href: str = '',
    tertiary_label: str = '',
):
    period_query = urlencode(_build_period_query_params(periodo, desde, hasta))
    resolved_secondary_route_name = secondary_route_name or (
        'dashboard_analytics' if critical_view else 'dashboard_offline_incidents'
    )
    resolved_secondary_label = secondary_label or (
        'Volver a analytics' if critical_view else 'Solo criticos'
    )
    context = {
        'offline_actions_title': title,
        'offline_actions_subtitle': subtitle,
        'offline_actions_force_render': force_render,
        'offline_actions_clear_href': f"{reverse(route_name)}?{period_query}",
        'offline_actions_critical_view': critical_view,
        'offline_actions_secondary_href': f"{reverse(resolved_secondary_route_name)}?{period_query}",
        'offline_actions_secondary_label': resolved_secondary_label,
        'offline_actions_tertiary_href': tertiary_href,
        'offline_actions_tertiary_label': tertiary_label,
    }
    if export_query_params:
        export_query = urlencode(export_query_params)
        export_json_route = (
            'dashboard_offline_incidents_export_json'
            if critical_view
            else (
                'dashboard_offline_retention_export_json'
                if route_name == 'dashboard_offline_retention'
                else 'dashboard_offline_audited_actions_export_json'
            )
        )
        export_csv_route = (
            'dashboard_offline_incidents_export_csv'
            if critical_view
            else (
                'dashboard_offline_retention_export_csv'
                if route_name == 'dashboard_offline_retention'
                else 'dashboard_offline_audited_actions_export_csv'
            )
        )
        context.update(
            {
                'offline_actions_export_json_href': f"{reverse(export_json_route)}?{export_query}",
                'offline_actions_export_csv_href': f"{reverse(export_csv_route)}?{export_query}",
            }
        )
    if critical_view:
        context.update(
            {
                'offline_actions_bulk_revalidate_href': reverse('dashboard_offline_incidents_bulk_revalidate_json'),
                'offline_actions_bulk_review_href': reverse('dashboard_offline_incidents_bulk_review_json'),
            }
        )
    return context


def _build_period_query_params(periodo, desde, hasta):
    params = {'periodo': periodo}
    if desde:
        params['desde'] = str(desde)
    if hasta:
        params['hasta'] = str(hasta)
    return params


def _build_offline_actions_query_params_from_context(context):
    params = _build_period_query_params(context['periodo'], context['desde'], context['hasta'])
    for field_name in (
        'offline_audited_action_filter_segment_id',
        'offline_audited_action_filter_time_window',
        'offline_audited_action_filter_type',
        'offline_audited_action_filter_organization',
        'offline_audited_action_filter_location',
        'offline_audited_action_filter_actor',
        'offline_audited_action_filter_segment_status',
        'offline_audited_action_filter_result',
        'offline_audited_action_filter_footer_presence',
        'offline_audited_action_filter_sort',
    ):
        value = str(context.get(field_name, '') or '').strip()
        if value:
            params[field_name.replace('offline_audited_action_filter_', 'offline_action_')] = value
    return params


def _build_offline_bulk_query_params_from_context(context):
    params = _build_period_query_params(context['periodo'], context['desde'], context['hasta'])
    mapping = {
        'offline_audited_action_filter_time_window': 'offline_action_time_window',
        'offline_audited_action_filter_organization': 'offline_action_organization',
        'offline_audited_action_filter_location': 'offline_action_location',
        'offline_audited_action_filter_actor': 'offline_action_actor',
    }
    for context_field, query_name in mapping.items():
        value = str(context.get(context_field, '') or '').strip()
        if value:
            params[query_name] = value
    return params


def _build_offline_bulk_back_to_incidents_query_params(context):
    params = _build_period_query_params(context['periodo'], context['desde'], context['hasta'])
    mapping = {
        'offline_bulk_action_filter_time_window': 'offline_action_time_window',
        'offline_bulk_action_filter_organization': 'offline_action_organization',
        'offline_bulk_action_filter_location': 'offline_action_location',
        'offline_bulk_action_filter_actor': 'offline_action_actor',
    }
    for context_field, query_name in mapping.items():
        value = str(context.get(context_field, '') or '').strip()
        if value:
            params[query_name] = value
    return params


def _build_offline_critical_incidents_export_payload_from_request(request):
    payload = build_offline_critical_incidents_export_payload(
        periodo=request.GET.get('periodo', 'semana'),
        desde_param=request.GET.get('desde'),
        hasta_param=request.GET.get('hasta'),
        offline_action_segment_id=request.GET.get('offline_action_segment_id', ''),
        offline_action_time_window=request.GET.get('offline_action_time_window', ''),
        offline_action_type=request.GET.get('offline_action_type', ''),
        offline_action_organization=request.GET.get('offline_action_organization', ''),
        offline_action_location=request.GET.get('offline_action_location', ''),
        offline_action_actor=request.GET.get('offline_action_actor', ''),
        offline_action_segment_status=request.GET.get('offline_action_segment_status', ''),
        offline_action_result=request.GET.get('offline_action_result', ''),
        offline_action_footer_presence=request.GET.get('offline_action_footer_presence', ''),
        offline_action_sort=request.GET.get('offline_action_sort', 'footer_missing'),
    )
    for item in payload['items']:
        item['auditlog_url'] = _build_auditlog_admin_href(item['audit_log_id'])
    return payload


def _build_offline_retention_actions_export_payload_from_request(request):
    payload = build_offline_retention_actions_export_payload(
        periodo=request.GET.get('periodo', 'semana'),
        desde_param=request.GET.get('desde'),
        hasta_param=request.GET.get('hasta'),
        offline_action_segment_id=request.GET.get('offline_action_segment_id', ''),
        offline_action_time_window=request.GET.get('offline_action_time_window', ''),
        offline_action_type=request.GET.get('offline_action_type', ''),
        offline_action_organization=request.GET.get('offline_action_organization', ''),
        offline_action_location=request.GET.get('offline_action_location', ''),
        offline_action_actor=request.GET.get('offline_action_actor', ''),
        offline_action_segment_status=request.GET.get('offline_action_segment_status', ''),
        offline_action_result=request.GET.get('offline_action_result', ''),
        offline_action_sort=request.GET.get('offline_action_sort', 'recent'),
    )
    for item in payload['items']:
        item['receipt_json_url'] = _build_offline_retention_receipt_json_href(item['audit_log_id'])
        item['auditlog_url'] = _build_auditlog_admin_href(item['audit_log_id'])
    return payload


def _build_offline_audited_actions_export_payload_from_request(request):
    payload = build_offline_audited_actions_export_payload(
        periodo=request.GET.get('periodo', 'semana'),
        desde_param=request.GET.get('desde'),
        hasta_param=request.GET.get('hasta'),
        offline_action_segment_id=request.GET.get('offline_action_segment_id', ''),
        offline_action_time_window=request.GET.get('offline_action_time_window', ''),
        offline_action_type=request.GET.get('offline_action_type', ''),
        offline_action_organization=request.GET.get('offline_action_organization', ''),
        offline_action_location=request.GET.get('offline_action_location', ''),
        offline_action_actor=request.GET.get('offline_action_actor', ''),
        offline_action_segment_status=request.GET.get('offline_action_segment_status', ''),
        offline_action_result=request.GET.get('offline_action_result', ''),
        offline_action_footer_presence=request.GET.get('offline_action_footer_presence', ''),
        offline_action_sort=request.GET.get('offline_action_sort', 'recent'),
    )
    for item in payload['items']:
        if item['event_type'] in {
            'offline.segment_usb_exported',
            'offline.segment_purged_after_usb',
        }:
            item['receipt_json_url'] = _build_offline_retention_receipt_json_href(item['audit_log_id'])
        else:
            item['receipt_json_url'] = ''
        item['auditlog_url'] = _build_auditlog_admin_href(item['audit_log_id'])
    return payload


def _build_offline_bulk_runs_export_payload_from_request(request):
    payload = build_offline_bulk_runs_export_payload(
        periodo=request.GET.get('periodo', 'semana'),
        desde_param=request.GET.get('desde'),
        hasta_param=request.GET.get('hasta'),
        offline_action_time_window=request.GET.get('offline_action_time_window', ''),
        offline_action_organization=request.GET.get('offline_action_organization', ''),
        offline_action_location=request.GET.get('offline_action_location', ''),
        offline_action_actor=request.GET.get('offline_action_actor', ''),
        offline_bulk_action_type=request.GET.get('offline_bulk_action_type', ''),
        offline_bulk_audit_log=request.GET.get('offline_bulk_audit_log', ''),
        offline_bulk_batch_id=request.GET.get('offline_bulk_batch_id', ''),
        offline_bulk_correlation_id=request.GET.get('offline_bulk_correlation_id', ''),
    )
    payload['items'] = [_enrich_offline_bulk_run_detail_urls(item) for item in payload['items']]
    if payload.get('selected_run'):
        payload['selected_run'] = _enrich_offline_bulk_run_detail_urls(payload['selected_run'])
    return payload


def _build_offline_bulk_export_query_params_from_context(context):
    params = _build_period_query_params(context['periodo'], context['desde'], context['hasta'])
    mapping = {
        'offline_bulk_action_filter_time_window': 'offline_action_time_window',
        'offline_bulk_action_filter_organization': 'offline_action_organization',
        'offline_bulk_action_filter_location': 'offline_action_location',
        'offline_bulk_action_filter_actor': 'offline_action_actor',
        'offline_bulk_action_filter_type': 'offline_bulk_action_type',
        'offline_bulk_action_filter_audit_log': 'offline_bulk_audit_log',
        'offline_bulk_action_filter_batch_id': 'offline_bulk_batch_id',
        'offline_bulk_action_filter_correlation_id': 'offline_bulk_correlation_id',
    }
    for context_field, query_name in mapping.items():
        value = str(context.get(context_field, '') or '').strip()
        if value:
            params[query_name] = value
    return params


def _build_offline_batch_json_href(audit_log_id='', *, batch_id='', correlation_id='') -> str:
    params = {}
    normalized_audit_log_id = str(audit_log_id or '').strip()
    normalized_batch_id = str(batch_id or '').strip()
    normalized_correlation_id = str(correlation_id or '').strip()
    if normalized_audit_log_id:
        params['audit_log_id'] = normalized_audit_log_id
    elif normalized_batch_id:
        params['batch_id'] = normalized_batch_id
    elif normalized_correlation_id:
        params['correlation_id'] = normalized_correlation_id
    return f"{reverse('dashboard_offline_incident_batch_json')}?{urlencode(params)}"


def _build_offline_retention_receipt_json_href(audit_log_id='') -> str:
    normalized_audit_log_id = str(audit_log_id or '').strip()
    return (
        f"{reverse('dashboard_offline_retention_receipt_json')}?"
        f"{urlencode({'audit_log_id': normalized_audit_log_id})}"
    )


def _build_offline_batch_html_href(audit_log_id='', *, batch_id='', correlation_id='') -> str:
    params = {}
    normalized_audit_log_id = str(audit_log_id or '').strip()
    normalized_batch_id = str(batch_id or '').strip()
    normalized_correlation_id = str(correlation_id or '').strip()
    if normalized_audit_log_id:
        params['audit_log_id'] = normalized_audit_log_id
    elif normalized_batch_id:
        params['batch_id'] = normalized_batch_id
    elif normalized_correlation_id:
        params['correlation_id'] = normalized_correlation_id
    return f"{reverse('dashboard_offline_incident_batch_detail')}?{urlencode(params)}"


def _build_offline_batch_back_href(request) -> str:
    params = {}
    for key in (
        'periodo',
        'desde',
        'hasta',
        'offline_action_time_window',
        'offline_action_organization',
        'offline_action_location',
        'offline_action_actor',
        'offline_bulk_action_type',
        'offline_bulk_audit_log',
        'offline_bulk_batch_id',
        'offline_bulk_correlation_id',
    ):
        value = str(request.GET.get(key, '') or '').strip()
        if value:
            params[key] = value
    base = reverse('dashboard_offline_incident_batches')
    return f"{base}?{urlencode(params)}" if params else base


def _build_offline_batch_segment_references(detail: dict) -> list[dict]:
    payload = dict(detail.get('payload_json') or {})
    failed_details = list(detail.get('failed_details') or [])
    segment_ids = []
    seen = set()

    def push(raw_segment_id):
        normalized = str(raw_segment_id or '').strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        segment_ids.append(normalized)

    for raw_segment_id in payload.get('segment_ids') or []:
        push(raw_segment_id)
    for raw_segment_id in payload.get('successful_segment_ids') or []:
        push(raw_segment_id)
    for raw_segment_id in payload.get('failed_segment_ids') or []:
        push(raw_segment_id)
    for item in failed_details:
        push((item or {}).get('segment_id'))

    failed_map = {}
    for item in failed_details:
        segment_id = str((item or {}).get('segment_id') or '').strip()
        if not segment_id:
            continue
        failed_map[segment_id] = str((item or {}).get('detail') or (item or {}).get('reason') or '').strip()

    successful_ids = {
        str(segment_id or '').strip()
        for segment_id in (payload.get('successful_segment_ids') or [])
        if str(segment_id or '').strip()
    }
    failed_ids = {
        str(segment_id or '').strip()
        for segment_id in (payload.get('failed_segment_ids') or [])
        if str(segment_id or '').strip()
    }
    latest_retention_by_segment = {}
    if segment_ids:
        retention_candidates = (
            AuditLog.objects.filter(
                target_model='OfflineJournalSegment',
                target_id__in=segment_ids,
                event_type__in=OFFLINE_RETENTION_EVENT_TYPES,
            )
            .order_by('target_id', '-created_at', '-id')
        )
        for audit in retention_candidates:
            if audit.target_id not in latest_retention_by_segment:
                latest_retention_by_segment[audit.target_id] = audit

    rows = []
    for segment_id in segment_ids:
        if segment_id in failed_ids:
            batch_status = 'failed'
        elif segment_id in successful_ids:
            batch_status = 'succeeded'
        else:
            batch_status = 'listed'
        encoded_segment_id = quote(segment_id, safe='')
        retention_audit = latest_retention_by_segment.get(segment_id)
        rows.append(
            {
                'segment_id': segment_id,
                'batch_status': batch_status,
                'detail': failed_map.get(segment_id, ''),
                'limbo_href': f'{reverse("dashboard_offline_limbo")}?segment_id={encoded_segment_id}',
                'html_href': f'{reverse("dashboard_offline_limbo_segment_detail")}?segment_id={encoded_segment_id}',
                'json_href': f'{reverse("dashboard_offline_limbo_segment_json")}?segment_id={encoded_segment_id}',
                'retention_receipt_json_href': (
                    _build_offline_retention_receipt_json_href(retention_audit.id) if retention_audit else ''
                ),
                'retention_audit_log_id': retention_audit.id if retention_audit else '',
                'retention_event_type': retention_audit.event_type if retention_audit else '',
                'retention_hint': _build_offline_batch_retention_hint(retention_audit),
            }
        )
    return rows


def _enrich_offline_batch_segments_with_live_state(rows: list[dict]) -> list[dict]:
    enriched_rows = []
    refreshed_at = timezone.now().isoformat()
    for row in rows:
        try:
            detail = build_offline_segment_detail_payload(row['segment_id'])
        except ValueError as exc:
            enriched_rows.append(
                {
                    **row,
                    'live_available': False,
                    'current_status': 'unavailable',
                    'current_detail': str(exc),
                    'current_footer_present': None,
                    'current_total_sales': 0,
                    'current_amount_total': '0.00',
                    'current_reviewed': False,
                    'current_refreshed_at': refreshed_at,
                }
            )
            continue

        summary = dict(detail.get('summary') or {})
        ops_metadata = dict(detail.get('ops_metadata') or {})
        enriched_rows.append(
            {
                **row,
                'live_available': True,
                'current_status': str(detail.get('status') or 'unavailable'),
                'current_detail': str(detail.get('detail') or ''),
                'current_footer_present': bool(detail.get('footer_present')),
                'current_total_sales': int(summary.get('total_sales') or 0),
                'current_amount_total': str(summary.get('amount_total') or '0.00'),
                'current_reviewed': bool(ops_metadata.get('operational_review')),
                'current_refreshed_at': str(detail.get('refreshed_at') or refreshed_at),
            }
        )
    return enriched_rows


def _build_offline_batch_segment_live_summary(rows: list[dict]) -> dict:
    summary = {
        'total_segments': len(rows),
        'resolved_segments': 0,
        'unavailable_segments': 0,
        'sealed_segments': 0,
        'footer_missing_segments': 0,
        'integrity_error_segments': 0,
        'open_segments': 0,
        'footer_present_segments': 0,
        'footer_absent_segments': 0,
        'reviewed_segments': 0,
        'unreviewed_segments': 0,
        'current_total_sales': 0,
        'current_amount_total': '0.00',
        'refreshed_at': timezone.now().isoformat(),
    }
    amount_total = Decimal('0.00')
    for row in rows:
        if not row.get('live_available'):
            summary['unavailable_segments'] += 1
            continue

        summary['resolved_segments'] += 1
        current_status = str(row.get('current_status') or '')
        if current_status == 'sealed':
            summary['sealed_segments'] += 1
        elif current_status == 'footer_missing':
            summary['footer_missing_segments'] += 1
        elif current_status == 'integrity_error':
            summary['integrity_error_segments'] += 1
        elif current_status == 'open':
            summary['open_segments'] += 1

        current_footer_present = row.get('current_footer_present')
        if current_footer_present is True:
            summary['footer_present_segments'] += 1
        elif current_footer_present is False:
            summary['footer_absent_segments'] += 1

        if row.get('current_reviewed'):
            summary['reviewed_segments'] += 1
        else:
            summary['unreviewed_segments'] += 1

        summary['current_total_sales'] += int(row.get('current_total_sales') or 0)
        try:
            amount_total += Decimal(str(row.get('current_amount_total') or '0'))
        except (InvalidOperation, TypeError, ValueError):
            amount_total += Decimal('0.00')

    summary['current_amount_total'] = f'{amount_total.quantize(Decimal("0.01")):.2f}'
    return summary


def _build_offline_segment_json_href(segment_id='') -> str:
    params = {}
    normalized_segment_id = str(segment_id or '').strip()
    if normalized_segment_id:
        params['segment_id'] = normalized_segment_id
    return f"{reverse('dashboard_offline_limbo_segment_json')}?{urlencode(params)}"


def _build_offline_segment_html_href(segment_id='') -> str:
    params = {}
    normalized_segment_id = str(segment_id or '').strip()
    if normalized_segment_id:
        params['segment_id'] = normalized_segment_id
    return f"{reverse('dashboard_offline_limbo_segment_detail')}?{urlencode(params)}"


def _build_offline_segment_back_href(request, segment_id='') -> str:
    params = {}
    normalized_segment_id = str(segment_id or '').strip()
    if normalized_segment_id:
        params['segment_id'] = normalized_segment_id
    base = reverse('dashboard_offline_limbo')
    return f"{base}?{urlencode(params)}" if params else base


def _execute_offline_segment_bulk_action_json(request, *, action: str):
    api_error = _require_admin_dashboard_api_access(request)
    if api_error:
        return api_error
    if request.method != 'POST':
        return JsonResponse({'detail': 'method not allowed'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'detail': 'json body invalido'}, status=400)

    segment_ids = body.get('segment_ids') or []
    try:
        payload = execute_offline_segment_bulk_action(
            action=action,
            segment_ids=segment_ids,
            user=request.user,
            ip_address=request.META.get('REMOTE_ADDR', ''),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )
    except OfflineLimboActionError as exc:
        return JsonResponse(
            {
                'detail': str(exc),
                'action': {
                    'name': action,
                    'performed': False,
                },
            },
            status=409,
        )

    status_code = 200 if payload['action']['succeeded'] > 0 else 409
    return JsonResponse(payload, status=status_code)
