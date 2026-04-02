from __future__ import annotations

from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.http import HttpResponseForbidden, JsonResponse

from pos.ledger_registry import MIN_SUPPORTED_QUEUE_SCHEMA, REGISTRY_VERSION, get_registry_hash
from pos.application.context import build_location_context, resolve_location_for_user
from pos.models import LedgerRegistryActivation


LEDGER_ACTIVATION_CACHE_KEY = 'pos:ledger-registry-activation'
MUTATING_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


def _forbidden_response(request, message: str):
    if request.path.startswith('/api/') or request.headers.get('Accept') == 'application/json':
        return JsonResponse({'status': 'error', 'mensaje': message}, status=403)
    return HttpResponseForbidden(message)


class CorrelationIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.correlation_id = request.headers.get('X-Request-ID') or uuid4().hex
        response = self.get_response(request)
        response['X-Request-ID'] = request.correlation_id
        return response


class LedgerRegistryFenceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.current_registry_hash = get_registry_hash()

    def __call__(self, request):
        if not self._should_enforce(request):
            response = self.get_response(request)
            return self._attach_runtime_headers(response)

        client_hash = (request.headers.get('X-Ledger-Registry-Hash') or '').strip()
        if not client_hash:
            return self._reject(
                status=403,
                code='ledger_identity_missing',
                message='X-Ledger-Registry-Hash requerido.',
            )

        if client_hash != self.current_registry_hash:
            return self._reject(
                status=426,
                code='ledger_registry_upgrade_required',
                message='El registry del cliente no coincide con el nodo activo.',
            )

        activation = self._get_activation()
        if not activation:
            return self._reject(
                status=503,
                code='ledger_activation_unavailable',
                message='No se pudo confirmar la activacion vigente del ledger.',
                draining=True,
            )

        if activation['maintenance_mode'] or activation['active_registry_hash'] != self.current_registry_hash:
            return self._reject(
                status=503,
                code='ledger_node_draining',
                message='El nodo esta drenando o el registry activo no coincide con este proceso.',
                draining=True,
            )

        response = self.get_response(request)
        return self._attach_runtime_headers(response)

    def _should_enforce(self, request) -> bool:
        if not getattr(settings, 'LEDGER_VERSION_FENCING_ENABLED', False):
            return False
        if request.method.upper() not in MUTATING_METHODS:
            return False
        fenced_paths = getattr(
            settings,
            'LEDGER_FENCED_MUTATION_PATHS',
            ('/registrar_venta/', '/api/reconciliar-pago/'),
        )
        return any(request.path.startswith(path) for path in fenced_paths)

    def _get_activation(self):
        cached = cache.get(LEDGER_ACTIVATION_CACHE_KEY)
        if cached:
            return cached

        try:
            activation = LedgerRegistryActivation.objects.only(
                'active_registry_version',
                'active_registry_hash',
                'min_supported_queue_schema',
                'maintenance_mode',
            ).get(singleton_key='default')
        except (LedgerRegistryActivation.DoesNotExist, DatabaseError):
            return None

        payload = {
            'active_registry_version': activation.active_registry_version,
            'active_registry_hash': activation.active_registry_hash,
            'min_supported_queue_schema': activation.min_supported_queue_schema,
            'maintenance_mode': activation.maintenance_mode,
        }
        cache.set(
            LEDGER_ACTIVATION_CACHE_KEY,
            payload,
            timeout=max(1, int(getattr(settings, 'LEDGER_ACTIVATION_CACHE_TTL_SECONDS', 5))),
        )
        return payload

    def _attach_runtime_headers(self, response):
        response['X-Ledger-Registry-Hash'] = self.current_registry_hash
        response['X-Ledger-Registry-Version'] = REGISTRY_VERSION
        response['X-Min-Supported-Queue-Schema'] = str(MIN_SUPPORTED_QUEUE_SCHEMA)
        return response

    def _reject(self, *, status: int, code: str, message: str, draining: bool = False):
        activation = self._get_activation() or {}
        payload = {
            'status': 'error',
            'code': code,
            'mensaje': message,
            'active_registry_hash': activation.get('active_registry_hash', self.current_registry_hash),
            'active_registry_version': activation.get('active_registry_version', REGISTRY_VERSION),
            'min_supported_queue_schema': activation.get(
                'min_supported_queue_schema',
                MIN_SUPPORTED_QUEUE_SCHEMA,
            ),
        }
        response = JsonResponse(payload, status=status)
        if draining:
            response['X-Bosco-Node-Status'] = 'Draining'
        return self._attach_runtime_headers(response)


class LocationContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        location_uuid = view_kwargs.get('location_uuid')
        if not location_uuid:
            return None

        try:
            location = resolve_location_for_user(request.user, location_uuid=location_uuid, allow_default=False)
        except PermissionDenied as exc:
            return _forbidden_response(request, str(exc))

        request.location_context = build_location_context(location)
        return None
