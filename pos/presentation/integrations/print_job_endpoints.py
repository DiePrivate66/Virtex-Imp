from __future__ import annotations

from django.http import JsonResponse

from pos.application.integrations import (
    IntegrationsError,
    acknowledge_print_job,
    fail_print_job,
    get_failed_print_jobs,
    get_pending_print_jobs,
    retry_print_job,
)

from ._common import ensure_authenticated
from .payloads import parse_json_body
from .responses import integration_error_response


def handle_pending_print_jobs_request(request):
    auth_error = ensure_authenticated(request)
    if auth_error:
        return auth_error
    return JsonResponse({'status': 'ok', 'jobs': get_pending_print_jobs()})


def handle_failed_print_jobs_request(request):
    auth_error = ensure_authenticated(request)
    if auth_error:
        return auth_error
    return JsonResponse({'status': 'ok', 'jobs': get_failed_print_jobs()})


def handle_print_job_ack_request(request, job_id: int):
    auth_error = ensure_authenticated(request)
    if auth_error:
        return auth_error

    data = parse_json_body(request)

    try:
        job = acknowledge_print_job(job_id, done=bool(data.get('done')))
        return JsonResponse({'status': 'ok', 'estado': job.estado})
    except IntegrationsError as exc:
        return integration_error_response(exc)


def handle_print_job_fail_request(request, job_id: int):
    auth_error = ensure_authenticated(request)
    if auth_error:
        return auth_error

    data = parse_json_body(request)

    try:
        job = fail_print_job(job_id, data.get('error'))
        return JsonResponse({'status': 'ok', 'estado': job.estado})
    except IntegrationsError as exc:
        return integration_error_response(exc)


def handle_print_job_retry_request(request, job_id: int):
    auth_error = ensure_authenticated(request)
    if auth_error:
        return auth_error

    try:
        job = retry_print_job(job_id)
        return JsonResponse({'status': 'ok', 'estado': job.estado})
    except IntegrationsError as exc:
        return integration_error_response(exc)
