from __future__ import annotations

from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .print_job_endpoints import (
    handle_failed_print_jobs_request,
    handle_pending_print_jobs_request,
    handle_print_job_ack_request,
    handle_print_job_fail_request,
    handle_print_job_retry_request,
)


@require_GET
def api_print_jobs_pending(request):
    return handle_pending_print_jobs_request(request)


@require_GET
def api_print_jobs_failed(request):
    return handle_failed_print_jobs_request(request)


@csrf_exempt
@require_POST
def api_print_job_ack(request, job_id: int):
    return handle_print_job_ack_request(request, job_id)


@csrf_exempt
@require_POST
def api_print_job_fail(request, job_id: int):
    return handle_print_job_fail_request(request, job_id)


@csrf_exempt
@require_POST
def api_print_job_retry(request, job_id: int):
    return handle_print_job_retry_request(request, job_id)
