from __future__ import annotations

import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render

from pos.application.context import resolve_location_for_user
from pos.application.sales import (
    PosSaleError,
    build_sale_response_payload,
    get_pos_home_context,
    get_user_open_cash_register,
    reconcile_payment_confirmation,
    register_sale,
)
from pos.application.sales.replay_admission import ReplayAdmissionError, admit_replay_request
from pos.application.staff import user_is_pos_operator
from pos.models import IdempotencyRecord

logger = logging.getLogger(__name__)


def _user_can_reconcile_payments(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if user.groups.filter(name='Admin').exists():
        return True
    empleado = getattr(user, 'empleado', None)
    return bool(empleado and empleado.rol == 'ADMIN')


def _resolve_replay_location(request):
    caja_abierta = get_user_open_cash_register(request.user)
    if caja_abierta and caja_abierta.location_id:
        return caja_abierta.location
    try:
        return resolve_location_for_user(request.user)
    except Exception:
        return None


def pos_index(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    caja_abierta = get_user_open_cash_register(request.user)
    if not caja_abierta:
        return redirect('pos_apertura')

    return render(request, 'pos/index.html', get_pos_home_context(request.user))


@login_required(login_url='pos_login')
def registrar_venta(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Metodo no permitido'}, status=405)
    if not user_is_pos_operator(request.user):
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=403)
    admission = None
    try:
        data = json.loads(request.body)
        admission = admit_replay_request(
            replay_header=request.headers.get('X-POS-Replay'),
            payload=data,
            location=_resolve_replay_location(request),
        )
        data.setdefault('correlation_id', getattr(request, 'correlation_id', ''))
        data.setdefault('audit_ip', request.META.get('REMOTE_ADDR', ''))
        data.setdefault('audit_user_agent', request.META.get('HTTP_USER_AGENT', ''))
        result = register_sale(request.user, data)
        payload = {
            'status': 'ok',
            'mensaje': (
                f'Venta #{result.venta.id} recuperada'
                if result.duplicated
                else f'Venta #{result.venta.id} registrada'
            ),
            **result.payload,
        }
        return admission.attach_headers(JsonResponse(payload)) if admission else JsonResponse(payload)
    except ReplayAdmissionError as exc:
        return exc.as_response()
    except PosSaleError as exc:
        response = JsonResponse(
            {
                'status': 'error',
                'mensaje': exc.message,
                **exc.extra_payload,
            },
            status=exc.status_code,
        )
        return admission.attach_headers(response) if admission else response
    except Exception:
        logger.exception('Error inesperado registrando venta POS')
        response = JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo registrar la venta. Intenta nuevamente.'},
            status=500,
        )
        return admission.attach_headers(response) if admission else response
    finally:
        if admission:
            admission.release()


@login_required(login_url='pos_login')
def consultar_transaccion_pendiente(request):
    client_transaction_id = (request.GET.get('client_transaction_id') or '').strip()
    location_uuid = request.GET.get('location_uuid')
    if not client_transaction_id:
        return JsonResponse({'status': 'error', 'mensaje': 'client_transaction_id requerido'}, status=400)

    try:
        location = resolve_location_for_user(request.user, location_uuid=location_uuid)
    except Exception:
        return JsonResponse({'status': 'error', 'mensaje': 'Sucursal no valida'}, status=403)

    record = (
        IdempotencyRecord.objects.select_related('venta')
        .filter(location=location, client_transaction_id=client_transaction_id)
        .first()
    )
    if not record:
        return JsonResponse({'status': 'not_found'})

    if record.venta_id:
        return JsonResponse(
            {
                'status': 'ok',
                'idempotency_status': record.status,
                **(record.response_payload or build_sale_response_payload(record.venta)),
            }
        )

    return JsonResponse({'status': 'ok', 'idempotency_status': record.status})


@login_required(login_url='pos_login')
def reconciliar_pago(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Metodo no permitido'}, status=405)
    if not _user_can_reconcile_payments(request.user):
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado para reconciliar pagos'}, status=403)
    admission = None
    try:
        data = json.loads(request.body)
        replay_location = _resolve_replay_location(request)
        admission = admit_replay_request(
            replay_header=request.headers.get('X-POS-Replay'),
            payload=data,
            location=replay_location,
        )
        result = reconcile_payment_confirmation(
            venta_id=data.get('venta_id'),
            client_transaction_id=data.get('client_transaction_id'),
            user=request.user,
            payment_reference=data.get('payment_reference') or data.get('referencia_pago', ''),
            payment_provider=data.get('payment_provider', ''),
            gateway_payload=data.get('gateway_payload') or {},
            location=replay_location,
        )
        return admission.attach_headers(JsonResponse({'status': 'ok', **result})) if admission else JsonResponse({'status': 'ok', **result})
    except ReplayAdmissionError as exc:
        return exc.as_response()
    except PosSaleError as exc:
        response = JsonResponse(
            {
                'status': 'error',
                'mensaje': exc.message,
                **exc.extra_payload,
            },
            status=exc.status_code,
        )
        return admission.attach_headers(response) if admission else response
    except Exception:
        logger.exception('Error inesperado reconciliando pago POS')
        response = JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo reconciliar el pago.'},
            status=500,
        )
        return admission.attach_headers(response) if admission else response
    finally:
        if admission:
            admission.release()
