from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import timedelta
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.template.loader import render_to_string
from django.utils import timezone

from pos.application.cash_register import (
    find_customer_by_identity_document,
    get_cash_available_on_turn,
    get_locked_open_cash_register_for_user,
    get_open_cash_register_for_user,
)
from pos.application.context import ensure_staff_profile_for_user, resolve_location_for_user
from pos.application.notifications import ResendEmailError, send_resend_email
from pos.infrastructure.tasks import process_outbox_event
from pos.models import (
    AccountingAdjustment,
    AuditLog,
    Cliente,
    DetalleVenta,
    IdempotencyRecord,
    Inventario,
    LocationInventory,
    MovimientoCaja,
    MovimientoInventario,
    OutboxEvent,
    Producto,
    Venta,
    compute_operating_day,
    ensure_system_ledger_account,
)

logger = logging.getLogger(__name__)


class RefundSettlementMode:
    CASH_DRAWER = 'CASH_DRAWER'
    EXTERNAL_REFUND = 'EXTERNAL_REFUND'
    ALL = {CASH_DRAWER, EXTERNAL_REFUND}


class PosSaleError(Exception):
    def __init__(self, message: str, status_code: int = 400, extra_payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra_payload = extra_payload or {}


@dataclass(frozen=True)
class SaleRegistrationResult:
    venta: Venta
    payload: dict
    duplicated: bool = False


def register_sale(user, data: dict) -> SaleRegistrationResult:
    turno_activo = get_open_cash_register_for_user(user)
    if not turno_activo:
        raise PosSaleError('No hay caja activa para registrar ventas', status_code=400)
    _ensure_cash_turn_is_usable(turno_activo)

    location = resolve_location_for_user(user, location_uuid=data.get('location_uuid'))
    operator = ensure_staff_profile_for_user(user, location=location)

    cliente = _resolve_customer(data)
    metodo_pago = (data.get('metodo_pago') or '').upper().strip()
    if metodo_pago not in {'EFECTIVO', 'TRANSFERENCIA', 'TARJETA'}:
        raise PosSaleError('Metodo de pago invalido', status_code=400)

    cart = data.get('carrito', [])
    if not cart:
        raise PosSaleError('El carrito esta vacio', status_code=400)

    validated_items, total_venta = _validate_and_price_cart(cart, organization=location.organization)
    referencia_pago = _normalize_reference(data.get('referencia_pago'))
    tarjeta_tipo = _normalize_simple_text(data.get('tarjeta_tipo'), 12)
    tarjeta_marca = _normalize_simple_text(data.get('tarjeta_marca'), 20)

    if metodo_pago == 'TARJETA':
        _validate_card_payment(total_venta, referencia_pago, tarjeta_tipo)

    client_transaction_id = _normalize_transaction_id(data.get('client_transaction_id'))
    if not client_transaction_id:
        client_transaction_id = uuid4().hex

    request_fingerprint = _build_request_fingerprint(
        location_id=location.id,
        cart=validated_items,
        cart_created_at=data.get('cart_created_at'),
        payment_method=metodo_pago,
        customer_id=cliente.id if cliente else None,
    )

    existing_completed = _get_completed_idempotent_sale(
        location_id=location.id,
        client_transaction_id=client_transaction_id,
        request_fingerprint=request_fingerprint,
    )
    if existing_completed:
        return SaleRegistrationResult(
            venta=existing_completed.venta,
            payload=existing_completed.response_payload or build_sale_response_payload(existing_completed.venta),
            duplicated=True,
        )

    venta = _reserve_sale(
        user=user,
        turno=turno_activo,
        location=location,
        operator=operator,
        cliente=cliente,
        data=data,
        metodo_pago=metodo_pago,
        referencia_pago=referencia_pago,
        tarjeta_tipo=tarjeta_tipo,
        tarjeta_marca=tarjeta_marca,
        client_transaction_id=client_transaction_id,
        request_fingerprint=request_fingerprint,
        validated_items=validated_items,
        total_venta=total_venta,
    )

    payment_result = _process_payment(
        metodo_pago=metodo_pago,
        total_venta=total_venta,
        referencia_pago=referencia_pago,
        tarjeta_tipo=tarjeta_tipo,
        data=data,
    )

    if payment_result['status'] != 'PAID':
        _mark_sale_payment_failed(
            venta_id=venta.id,
            user=user,
            reason=payment_result.get('reason', 'Pago no autorizado'),
        )
        raise PosSaleError(payment_result.get('reason', 'Pago no autorizado'), status_code=400)

    venta = _mark_sale_paid(
        venta_id=venta.id,
        user=user,
        payment_reference=payment_result.get('payment_reference', referencia_pago),
        payment_provider=payment_result.get('payment_provider', ''),
    )

    if cliente and cliente.email:
        send_sale_receipt_email_async(venta, cliente.email)

    payload = build_sale_response_payload(venta)
    return SaleRegistrationResult(venta=venta, payload=payload, duplicated=False)


def expire_stale_pending_sales(*, stale_before=None, limit: int = 200) -> dict:
    batch_limit = min(max(1, limit), int(getattr(settings, 'PENDING_PAYMENT_REAPER_BATCH_SIZE', 50)))
    cutoff = stale_before or (
        timezone.now() - timedelta(seconds=max(60, int(getattr(settings, 'PENDING_PAYMENT_TIMEOUT_SECONDS', 600))))
    )
    candidate_ids = list(
        Venta.objects.filter(
            payment_status=Venta.PaymentStatus.PENDING,
            fecha__lte=cutoff,
        )
        .order_by('fecha')
        .values_list('id', flat=True)[:batch_limit]
    )

    expired_ids: list[int] = []
    skipped_ids: list[int] = []
    for venta_id in candidate_ids:
        if _mark_sale_payment_failed(
            venta_id=venta_id,
            user=None,
            reason='Reserva expirada por timeout de pago',
            failure_status=Venta.PaymentStatus.VOIDED,
            stale_before=cutoff,
            skip_locked=True,
        ):
            expired_ids.append(venta_id)
        else:
            skipped_ids.append(venta_id)

    return {
        'cutoff': cutoff.isoformat(),
        'expired_count': len(expired_ids),
        'expired_ids': expired_ids,
        'skipped_ids': skipped_ids,
    }


def purge_expired_idempotency_records(*, purge_before=None, limit: int = 1000) -> dict:
    cutoff = purge_before or (
        timezone.now() - timedelta(hours=max(24, int(getattr(settings, 'IDEMPOTENCY_PURGE_AFTER_HOURS', 48))))
    )
    batch_limit = min(max(1, limit), int(getattr(settings, 'IDEMPOTENCY_PURGE_BATCH_SIZE', 1000)))
    queryset = IdempotencyRecord.objects.filter(
        expires_at__lte=cutoff,
        status__in=[
            IdempotencyRecord.Status.COMPLETED,
            IdempotencyRecord.Status.FAILED_FINAL,
            IdempotencyRecord.Status.FAILED_RETRYABLE,
        ],
    ).order_by('expires_at')
    candidate_ids = list(queryset.values_list('id', flat=True)[:batch_limit])
    if not candidate_ids:
        return {'cutoff': cutoff.isoformat(), 'purged_count': 0, 'purged_ids': []}

    deleted_count, _ = IdempotencyRecord.objects.filter(id__in=candidate_ids).delete()
    return {
        'cutoff': cutoff.isoformat(),
        'purged_count': deleted_count,
        'purged_ids': candidate_ids,
    }


def reconcile_payment_confirmation(
    *,
    venta_id: int | None = None,
    client_transaction_id: str | None = None,
    user=None,
    payment_reference: str = '',
    payment_provider: str = '',
    gateway_payload: dict | None = None,
) -> dict:
    if not venta_id and not client_transaction_id:
        raise PosSaleError('Debe indicar venta_id o client_transaction_id', status_code=400)

    lookup = {'id': venta_id} if venta_id else {'client_transaction_id': _normalize_transaction_id(client_transaction_id)}
    venta = (
        Venta.objects.select_related('location', 'organization', 'operator')
        .filter(**lookup)
        .first()
    )
    if not venta:
        raise PosSaleError('Venta no encontrada para reconciliacion', status_code=404)

    if venta.payment_status == Venta.PaymentStatus.PAID:
        return {
            'status': 'already_paid',
            'message': f'La venta #{venta.id} ya estaba pagada.',
            'payload': build_sale_response_payload(venta),
        }

    if venta.payment_status == Venta.PaymentStatus.PENDING:
        venta = _mark_sale_paid(
            venta_id=venta.id,
            user=user,
            payment_reference=payment_reference,
            payment_provider=payment_provider or 'MANUAL_RECONCILIATION',
        )
        return {
            'status': 'paid',
            'message': f'La venta #{venta.id} se confirmo desde reconciliacion.',
            'payload': build_sale_response_payload(venta),
        }

    with transaction.atomic():
        venta = (
            Venta.objects.select_for_update()
            .select_related('location', 'organization', 'operator')
            .get(id=venta.id)
        )
        inventory_snapshot = _build_inventory_snapshot_for_sale(venta)
        alert_log = AuditLog.objects.create(
            organization=venta.organization,
            location=venta.location,
            actor_user=user,
            actor_staff=venta.operator,
            event_type='sale.orphan_payment_detected',
            target_model='Venta',
            target_id=str(venta.id),
            requires_attention=True,
            payload_json={
                'payment_status': venta.payment_status,
                'payment_reference': payment_reference,
                'payment_provider': payment_provider or 'UNKNOWN',
                'gateway_payload': gateway_payload or {},
                'action_source': 'LATE_PAYMENT_RECONCILIATION',
                'inventory_snapshot': inventory_snapshot,
            },
            correlation_id=venta.client_transaction_id,
        )
        outbox_event = OutboxEvent.objects.create(
            organization=venta.organization,
            location=venta.location,
            aggregate_type='Venta',
            aggregate_id=str(venta.id),
            event_type='ADMIN_EXCEPTION_ALERT',
            payload_json={
                'alert_type': 'ORPHAN_PAYMENT',
                'venta_id': venta.id,
                'audit_log_id': alert_log.id,
                'total': f'{venta.total:.2f}',
                'location_name': venta.location.name if venta.location_id else '',
                'payment_reference': payment_reference,
                'payment_provider': payment_provider or 'UNKNOWN',
                'client_transaction_id': venta.client_transaction_id,
            },
            correlation_id=venta.client_transaction_id,
            priority=OutboxEvent.Priority.CRITICAL,
            status=OutboxEvent.Status.PENDING,
        )
        transaction.on_commit(lambda event_id=outbox_event.id: process_outbox_event.delay(event_id))

    return {
        'status': 'manual_review_required',
        'message': f'La venta #{venta.id} recibio un pago tardio y requiere reconciliacion manual.',
        'payload': build_sale_response_payload(venta),
    }


class PaymentExceptionResolutionAction:
    REACTIVATE_SALE = 'REACTIVATE_SALE'
    REGISTER_INCOME_ONLY = 'REGISTER_INCOME_ONLY'
    REFUND_REQUIRED = 'REFUND_REQUIRED'

    ALL = {
        REACTIVATE_SALE,
        REGISTER_INCOME_ONLY,
        REFUND_REQUIRED,
    }


def resolve_payment_exception(
    *,
    audit_log_id: int,
    user,
    resolution_note: str = '',
    resolution_action: str = PaymentExceptionResolutionAction.REGISTER_INCOME_ONLY,
    resolution_reference: str = '',
) -> AuditLog:
    resolution_action = (resolution_action or '').strip().upper()
    if resolution_action not in PaymentExceptionResolutionAction.ALL:
        raise PosSaleError('Accion de resolucion invalida', status_code=400)

    resolution_note = (resolution_note or '').strip()
    if not resolution_note:
        raise PosSaleError('La justificacion de resolucion es obligatoria', status_code=400)

    with transaction.atomic():
        alert = (
            AuditLog.objects.select_for_update()
            .select_related('organization', 'location')
            .filter(
                id=audit_log_id,
                event_type='sale.orphan_payment_detected',
                requires_attention=True,
                resolved_at__isnull=True,
            )
            .first()
        )
        if not alert:
            raise PosSaleError('La excepcion indicada no existe o ya fue resuelta', status_code=404)

        venta = (
            Venta.objects.select_for_update()
            .select_related('location', 'organization', 'operator', 'turno')
            .filter(id=int(alert.target_id))
            .first()
        )
        if not venta:
            raise PosSaleError('La venta asociada a la excepcion ya no existe', status_code=404)

        action_payload = {
            'resolution_action': resolution_action,
            'resolution_note': resolution_note[:255],
            'resolution_reference': (resolution_reference or '').strip()[:80],
            'resolved_alert_event_type': alert.event_type,
        }

        if resolution_action == PaymentExceptionResolutionAction.REACTIVATE_SALE:
            if venta.payment_status not in {Venta.PaymentStatus.VOIDED, Venta.PaymentStatus.FAILED}:
                raise PosSaleError(
                    f'La venta #{venta.id} ya no puede reactivarse porque esta {venta.payment_status.lower()}',
                    status_code=409,
                )
            _reserve_inventory_for_existing_sale(venta=venta, registrado_por=user)
            _finalize_sale_as_paid(
                venta=venta,
                user=user,
                payment_reference=alert.payload_json.get('payment_reference', ''),
                payment_provider=alert.payload_json.get('payment_provider', 'MANUAL_RECONCILIATION'),
                audit_event_type='sale.payment_reactivated',
            )
            action_payload['reactivated_sale_id'] = venta.id
        elif resolution_action == PaymentExceptionResolutionAction.REGISTER_INCOME_ONLY:
            adjustment = _create_accounting_adjustment_for_orphan_payment(
                venta=venta,
                alert=alert,
                user=user,
                adjustment_type=AccountingAdjustment.AdjustmentType.ORPHAN_PAYMENT_UNIDENTIFIED,
                account_bucket=AccountingAdjustment.AccountBucket.PENDING_IDENTIFICATION,
                source_account_code=AccountingAdjustment.SystemLedgerCode.PAYMENT_GATEWAY_CLEARING,
                destination_account_code=AccountingAdjustment.SystemLedgerCode.UNIDENTIFIED_RECEIPTS,
                resolution_note=resolution_note,
                resolution_reference=resolution_reference,
            )
            action_payload['accounting_adjustment_id'] = adjustment.id
            action_payload['account_bucket'] = adjustment.account_bucket
        elif resolution_action == PaymentExceptionResolutionAction.REFUND_REQUIRED:
            adjustment = _create_accounting_adjustment_for_orphan_payment(
                venta=venta,
                alert=alert,
                user=user,
                adjustment_type=AccountingAdjustment.AdjustmentType.ORPHAN_PAYMENT_REFUND_PENDING,
                account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY,
                source_account_code=AccountingAdjustment.SystemLedgerCode.PAYMENT_GATEWAY_CLEARING,
                destination_account_code=AccountingAdjustment.SystemLedgerCode.REFUND_PAYABLE,
                resolution_note=resolution_note,
                resolution_reference=resolution_reference,
            )
            action_payload['accounting_adjustment_id'] = adjustment.id
            action_payload['account_bucket'] = adjustment.account_bucket

        alert.resolved_at = timezone.now()
        alert.resolved_by = user
        alert.requires_attention = False
        alert.save(update_fields=['resolved_at', 'resolved_by', 'requires_attention'])

        AuditLog.objects.create(
            organization=alert.organization,
            location=alert.location,
            actor_user=user,
            event_type='sale.orphan_payment_resolved',
            target_model='AuditLog',
            target_id=str(alert.id),
            payload_json=action_payload,
            correlation_id=alert.correlation_id,
        )
        return alert


@transaction.atomic
def resolve_accounting_adjustment(
    *,
    adjustment_id: int,
    user,
    resolution_note: str,
    resolution_reference: str = '',
    settlement_mode: str = RefundSettlementMode.CASH_DRAWER,
) -> AccountingAdjustment:
    resolution_note = (resolution_note or '').strip()
    if not resolution_note:
        raise PosSaleError('La justificacion del ajuste es obligatoria', status_code=400)

    resolution_reference = (resolution_reference or '').strip()[:80]
    settlement_mode = (settlement_mode or RefundSettlementMode.CASH_DRAWER).strip().upper()
    if settlement_mode not in RefundSettlementMode.ALL:
        raise PosSaleError('Modo de liquidacion invalido', status_code=400)

    adjustment = (
        AccountingAdjustment.objects.select_for_update()
        .select_related(
            'organization',
            'location',
            'sale',
            'source_audit_log',
            'source_account',
            'destination_account',
        )
        .filter(
            id=adjustment_id,
            status=AccountingAdjustment.Status.OPEN,
        )
        .first()
    )
    if not adjustment:
        raise PosSaleError('El ajuste contable indicado no existe o ya fue resuelto', status_code=404)

    if adjustment.account_bucket != AccountingAdjustment.AccountBucket.REFUND_LIABILITY:
        raise PosSaleError('Solo los reembolsos pendientes requieren resolucion manual', status_code=400)

    execution_turn = None
    cash_movement = None
    if settlement_mode == RefundSettlementMode.CASH_DRAWER:
        execution_turn = get_locked_open_cash_register_for_user(user)
        if not execution_turn or execution_turn.fecha_cierre is not None:
            raise PosSaleError(
                'Necesitas una caja abierta para ejecutar el reembolso desde el cajon.',
                status_code=409,
            )
        if adjustment.organization_id and execution_turn.organization_id and execution_turn.organization_id != adjustment.organization_id:
            raise PosSaleError(
                'La caja abierta no pertenece a la misma organizacion del ajuste a resolver',
                status_code=409,
            )
        if adjustment.location_id and execution_turn.location_id and execution_turn.location_id != adjustment.location_id:
            raise PosSaleError(
                'La caja abierta no pertenece a la misma sucursal del ajuste a resolver',
                status_code=409,
            )
        available_cash = get_cash_available_on_turn(execution_turn)
        if adjustment.amount > available_cash:
            raise PosSaleError(
                (
                    f'No hay efectivo suficiente en caja para devolver ${adjustment.amount:.2f}. '
                    f'Disponible actual: ${available_cash:.2f}. '
                    'Registra un aporte de efectivo o usa el modo de reembolso externo.'
                ),
                status_code=409,
                extra_payload={
                    'available_cash': f'{available_cash:.2f}',
                    'required_cash': f'{adjustment.amount:.2f}',
                    'suggested_settlement_mode': RefundSettlementMode.EXTERNAL_REFUND,
                },
            )
        cash_movement = MovimientoCaja.objects.create(
            turno=execution_turn,
            organization=adjustment.organization,
            location=adjustment.location or execution_turn.location,
            operator=ensure_staff_profile_for_user(user, location=execution_turn.location or adjustment.location),
            tipo='EGRESO',
            concepto=MovimientoCaja.CONCEPTO_REEMBOLSO_HEREDADO,
            descripcion=(
                f'Reembolso heredado ajuste #{adjustment.id}'
                f' de venta #{adjustment.sale_id or "N/A"}'
                f' (turno origen #{adjustment.sale.turno_id if adjustment.sale_id and adjustment.sale and adjustment.sale.turno_id else "N/A"})'
            )[:200],
            monto=adjustment.amount,
            registrado_por=user,
        )

    adjustment.status = AccountingAdjustment.Status.RESOLVED
    if resolution_reference:
        adjustment.external_reference = resolution_reference
    adjustment.save(update_fields=['status', 'external_reference', 'updated_at'])

    AuditLog.objects.create(
        organization=adjustment.organization,
        location=adjustment.location,
        actor_user=user,
        event_type='accounting.adjustment_resolved',
        target_model='AccountingAdjustment',
        target_id=str(adjustment.id),
        payload_json={
            'adjustment_type': adjustment.adjustment_type,
            'account_bucket': adjustment.account_bucket,
            'source_account': adjustment.source_account.code,
            'destination_account': adjustment.destination_account.code,
            'source_account_name': adjustment.source_account.name,
            'destination_account_name': adjustment.destination_account.name,
            'sale_id': adjustment.sale_id,
            'source_audit_log_id': adjustment.source_audit_log_id,
            'resolution_note': resolution_note[:255],
            'resolution_reference': resolution_reference,
            'resolution_action': 'REFUND_COMPLETED',
            'settlement_mode': settlement_mode,
            'execution_turn_id': execution_turn.id if execution_turn else None,
            'cash_movement_id': cash_movement.id if cash_movement else None,
            'cash_impact_recorded': bool(cash_movement),
        },
        correlation_id=adjustment.correlation_id,
    )
    return adjustment


def build_sale_response_payload(venta: Venta) -> dict:
    outbox_status = (
        OutboxEvent.objects.filter(
            aggregate_type='Venta',
            aggregate_id=str(venta.id),
            event_type='SALE_PAID_PRINT',
        )
        .values_list('status', flat=True)
        .first()
    ) or ''
    return {
        'ticket_id': venta.id,
        'venta_id': venta.id,
        'folio': venta.id,
        'total': f'{venta.total:.2f}',
        'payment_status': venta.payment_status,
        'estado': venta.estado,
        'client_transaction_id': venta.client_transaction_id,
        'outbox_status': outbox_status,
        'requires_new_transaction_id': venta.payment_status in {Venta.PaymentStatus.FAILED, Venta.PaymentStatus.VOIDED},
    }


def _reserve_sale(
    *,
    user,
    turno,
    location,
    operator,
    cliente,
    data: dict,
    metodo_pago: str,
    referencia_pago: str,
    tarjeta_tipo: str,
    tarjeta_marca: str,
    client_transaction_id: str,
    request_fingerprint: str,
    validated_items: list[dict],
    total_venta: Decimal,
) -> Venta:
    with transaction.atomic():
        record, created = IdempotencyRecord.objects.select_for_update().get_or_create(
            location=location,
            client_transaction_id=client_transaction_id,
            defaults={
                'organization': location.organization,
                'request_fingerprint': request_fingerprint,
                'status': IdempotencyRecord.Status.PENDING,
                'expires_at': timezone.now() + timedelta(hours=int(getattr(settings, 'IDEMPOTENCY_TTL_HOURS', 24))),
            },
        )
        if not created:
            if record.request_fingerprint and record.request_fingerprint != request_fingerprint:
                raise PosSaleError('La transaccion ya existe con un carrito distinto', status_code=409)
            if record.status == IdempotencyRecord.Status.COMPLETED and record.venta_id:
                return record.venta
            if record.status == IdempotencyRecord.Status.FAILED_FINAL:
                payload = record.response_payload or (
                    build_sale_response_payload(record.venta) if record.venta_id else {'requires_new_transaction_id': True}
                )
                raise PosSaleError(
                    'La transaccion anterior expiro o fallo. Genera un nuevo intento de cobro.',
                    status_code=409,
                    extra_payload=payload,
                )
            raise PosSaleError('La venta ya se esta procesando, verifica el estado antes de reenviar', status_code=409)

        venta = Venta.objects.create(
            cliente_nombre=data.get('cliente_nombre', 'CONSUMIDOR FINAL'),
            cliente=cliente,
            metodo_pago=metodo_pago,
            referencia_pago=referencia_pago,
            tarjeta_tipo=tarjeta_tipo,
            tarjeta_marca=tarjeta_marca,
            total=total_venta,
            origen='POS',
            estado='PENDIENTE',
            tipo_pedido=data.get('tipo_pedido', 'SERVIR'),
            monto_recibido=_parse_decimal(data.get('monto_recibido', 0)),
            turno=turno,
            organization=location.organization,
            location=location,
            operator=operator,
            operating_day=turno.operating_day,
            client_transaction_id=client_transaction_id,
            payment_status=Venta.PaymentStatus.PENDING,
            payment_method_type=metodo_pago,
            payment_reference=referencia_pago,
        )

        for item in validated_items:
            DetalleVenta.objects.create(
                venta=venta,
                producto=item['producto'],
                cantidad=item['cantidad'],
                precio_unitario=item['precio_unitario'],
                precio_bruto_unitario=item['precio_bruto_unitario'],
                descuento_monto=item['descuento_monto'],
                impuesto_monto=item['impuesto_monto'],
                subtotal_neto=item['subtotal_neto'],
                pricing_rule_snapshot=item['pricing_rule_snapshot'],
                tax_rule_snapshot=item['tax_rule_snapshot'],
                discount_rule_snapshot=item['discount_rule_snapshot'],
                nota=item['nota'],
            )

        _reserve_inventory(location=location, venta=venta, items=validated_items, registrado_por=user)

        record.venta = venta
        record.response_payload = build_sale_response_payload(venta)
        record.save(update_fields=['venta', 'response_payload', 'updated_at'])
        return venta


def _finalize_sale_as_paid(*, venta: Venta, user, payment_reference: str, payment_provider: str, audit_event_type: str) -> Venta:
    if venta.turno_id:
        venta.turno = venta.turno.__class__.objects.select_for_update().get(id=venta.turno_id)

    venta.payment_status = Venta.PaymentStatus.PAID
    venta.payment_failure_reason = ''
    venta.payment_reference = payment_reference or venta.payment_reference or venta.referencia_pago
    venta.payment_provider = payment_provider or venta.payment_provider
    venta.payment_checked_at = timezone.now()
    venta.estado = 'COCINA'
    venta.save(
        update_fields=[
            'payment_status',
            'payment_failure_reason',
            'payment_reference',
            'payment_provider',
            'payment_checked_at',
            'estado_pago',
            'estado',
        ]
    )

    MovimientoCaja.objects.create(
        turno=venta.turno,
        organization=venta.organization,
        location=venta.location,
        operator=venta.operator,
        tipo='INGRESO',
        concepto='VENTA',
        descripcion=f'Venta #{venta.id} ({venta.payment_method_type or venta.metodo_pago})',
        monto=venta.total,
        registrado_por=user,
    )

    outbox_event = OutboxEvent.objects.create(
        organization=venta.organization,
        location=venta.location,
        aggregate_type='Venta',
        aggregate_id=str(venta.id),
        event_type='SALE_PAID_PRINT',
        payload_json={
            'venta_id': venta.id,
            'print_types': ['COMANDA', 'TICKET'],
        },
        correlation_id=venta.client_transaction_id or uuid4().hex,
        priority=OutboxEvent.Priority.HIGH,
        status=OutboxEvent.Status.PENDING,
    )
    transaction.on_commit(lambda event_id=outbox_event.id: process_outbox_event.delay(event_id))

    AuditLog.objects.create(
        organization=venta.organization,
        location=venta.location,
        actor_user=user,
        actor_staff=venta.operator,
        event_type=audit_event_type,
        target_model='Venta',
        target_id=str(venta.id),
        payload_json={
            'payment_status': venta.payment_status,
            'payment_method_type': venta.payment_method_type,
            'payment_reference': venta.payment_reference,
        },
        correlation_id=venta.client_transaction_id,
    )

    IdempotencyRecord.objects.filter(location=venta.location, client_transaction_id=venta.client_transaction_id).update(
        status=IdempotencyRecord.Status.COMPLETED,
        response_payload=build_sale_response_payload(venta),
        updated_at=timezone.now(),
    )
    return venta


def _create_accounting_adjustment_for_orphan_payment(
    *,
    venta: Venta,
    alert: AuditLog,
    user,
    adjustment_type: str,
    account_bucket: str,
    source_account_code: str,
    destination_account_code: str,
    resolution_note: str,
    resolution_reference: str,
) -> AccountingAdjustment:
    source_account = ensure_system_ledger_account(
        organization=venta.organization,
        system_code=source_account_code,
    )
    destination_account = ensure_system_ledger_account(
        organization=venta.organization,
        system_code=destination_account_code,
    )
    return AccountingAdjustment.objects.create(
        organization=venta.organization,
        location=venta.location,
        sale=venta,
        source_audit_log=alert,
        adjustment_type=adjustment_type,
        account_bucket=account_bucket,
        source_account=source_account,
        destination_account=destination_account,
        status=AccountingAdjustment.Status.OPEN,
        amount=venta.total,
        operating_day=venta.operating_day,
        effective_at=timezone.now(),
        payment_reference=alert.payload_json.get('payment_reference', '')[:80],
        payment_provider=alert.payload_json.get('payment_provider', '')[:50],
        external_reference=(resolution_reference or '').strip()[:80],
        note=(resolution_note or '').strip()[:255],
        correlation_id=alert.correlation_id,
        created_by=user,
    )


def _mark_sale_paid(*, venta_id: int, user, payment_reference: str, payment_provider: str) -> Venta:
    with transaction.atomic():
        venta = Venta.objects.select_for_update().select_related('location', 'organization', 'operator').get(id=venta_id)
        if venta.payment_status == Venta.PaymentStatus.PAID:
            return venta
        if venta.payment_status != Venta.PaymentStatus.PENDING:
            raise PosSaleError(
                f'La venta #{venta.id} ya no puede confirmarse porque fue {venta.payment_status.lower()}',
                status_code=409,
            )

        _finalize_sale_as_paid(
            venta=venta,
            user=user,
            payment_reference=payment_reference,
            payment_provider=payment_provider,
            audit_event_type='sale.payment_confirmed',
        )
        return venta


def _reserve_inventory_for_existing_sale(*, venta: Venta, registrado_por):
    items = [
        {
            'producto': detalle.producto,
            'cantidad': detalle.cantidad,
        }
        for detalle in venta.detalles.select_related('producto').all()
    ]
    if not items:
        raise PosSaleError('La venta no tiene items para reactivar', status_code=409)
    _reserve_inventory(location=venta.location, venta=venta, items=items, registrado_por=registrado_por)


def _mark_sale_payment_failed(
    *,
    venta_id: int,
    user,
    reason: str,
    failure_status: str = Venta.PaymentStatus.FAILED,
    stale_before=None,
    skip_locked: bool = False,
) -> bool:
    with transaction.atomic():
        lock_kwargs = {'skip_locked': True} if skip_locked and connection.features.has_select_for_update_skip_locked else {}
        venta = (
            Venta.objects.select_for_update(**lock_kwargs)
            .select_related('location', 'organization', 'operator')
            .filter(id=venta_id)
            .first()
        )
        if not venta:
            return False
        if venta.payment_status == Venta.PaymentStatus.PAID:
            return False
        if venta.payment_status in {Venta.PaymentStatus.FAILED, Venta.PaymentStatus.VOIDED}:
            return False
        if stale_before and venta.fecha > stale_before:
            return False

        _restore_inventory(venta=venta, registrado_por=user)

        venta.payment_status = failure_status
        venta.payment_failure_reason = reason[:255]
        venta.payment_checked_at = timezone.now()
        venta.estado = 'CANCELADO'
        venta.save(
            update_fields=[
                'payment_status',
                'payment_failure_reason',
                'payment_checked_at',
                'estado_pago',
                'estado',
            ]
        )

        AuditLog.objects.create(
            organization=venta.organization,
            location=venta.location,
            actor_user=user,
            actor_staff=venta.operator,
            event_type='sale.payment_failed' if failure_status == Venta.PaymentStatus.FAILED else 'sale.payment_voided',
            target_model='Venta',
            target_id=str(venta.id),
            payload_json={
                'payment_status': venta.payment_status,
                'payment_failure_reason': venta.payment_failure_reason,
                'action_source': (
                    'AUTO_EXPIRATION_BY_REAPER'
                    if user is None and failure_status == Venta.PaymentStatus.VOIDED
                    else 'PAYMENT_FAILURE'
                ),
            },
            correlation_id=venta.client_transaction_id,
        )

        IdempotencyRecord.objects.filter(location=venta.location, client_transaction_id=venta.client_transaction_id).update(
            status=IdempotencyRecord.Status.FAILED_FINAL,
            response_payload=build_sale_response_payload(venta),
            updated_at=timezone.now(),
        )
        return True


def _resolve_customer(data: dict):
    cedula_input = (data.get('cliente_cedula') or '').strip()
    consumidor_final = bool(data.get('consumidor_final'))
    if consumidor_final:
        return None

    if data.get('cliente_id'):
        cliente = Cliente.objects.filter(id=data.get('cliente_id')).first()
        if not cliente:
            raise PosSaleError('Cliente no encontrado', status_code=400)
        if not _is_valid_identity(cliente.cedula_ruc):
            raise PosSaleError('C.I/RUC invalido (10 o 13 digitos)', status_code=400)
        return cliente

    if not cedula_input:
        return None

    if not _is_valid_identity(cedula_input):
        raise PosSaleError('C.I/RUC invalido (10 o 13 digitos)', status_code=400)

    return find_customer_by_identity_document(cedula_input)


def _validate_and_price_cart(cart: list[dict], *, organization) -> tuple[list[dict], Decimal]:
    total = Decimal('0.00')
    validated_items: list[dict] = []
    for item in cart:
        try:
            producto = Producto.objects.get(id=item['id'], organization=organization, activo=True)
        except (KeyError, Producto.DoesNotExist) as exc:
            raise PosSaleError('Producto no encontrado o no disponible', status_code=400) from exc

        cantidad = int(item.get('cantidad', 1))
        if cantidad <= 0:
            raise PosSaleError('Cantidad invalida en el carrito', status_code=400)

        precio_unitario = Decimal(str(producto.precio)).quantize(Decimal('0.01'))
        subtotal = (precio_unitario * cantidad).quantize(Decimal('0.01'))
        total += subtotal
        validated_items.append(
            {
                'producto': producto,
                'cantidad': cantidad,
                'precio_unitario': precio_unitario,
                'precio_bruto_unitario': precio_unitario,
                'descuento_monto': Decimal('0.00'),
                'impuesto_monto': Decimal('0.00'),
                'subtotal_neto': subtotal,
                'pricing_rule_snapshot': {'source': 'product.precio', 'product_price': f'{precio_unitario:.2f}'},
                'tax_rule_snapshot': {},
                'discount_rule_snapshot': {},
                'nota': _build_sale_note(producto.nombre, item.get('nombre', producto.nombre), item.get('nota', '')),
            }
        )

    if total <= 0:
        raise PosSaleError('El total de la venta debe ser mayor a 0', status_code=400)
    return validated_items, total.quantize(Decimal('0.01'))


def _reserve_inventory(*, location, venta: Venta, items: list[dict], registrado_por):
    product_ids = sorted({item['producto'].id for item in items})
    location_inventory_by_product = {
        inventory.producto_id: inventory
        for inventory in LocationInventory.objects.select_for_update().filter(
            location=location,
            producto_id__in=product_ids,
        ).order_by('producto_id')
    }

    for product_id in product_ids:
        if product_id not in location_inventory_by_product:
            legacy_inventory = Inventario.objects.filter(producto_id=product_id).first()
            location_inventory_by_product[product_id] = LocationInventory.objects.create(
                location=location,
                producto_id=product_id,
                stock_actual=legacy_inventory.stock_actual if legacy_inventory else 0,
                stock_minimo=legacy_inventory.stock_minimo if legacy_inventory else 5,
                unidad=legacy_inventory.unidad if legacy_inventory else 'unidades',
            )

    aggregated_items: dict[int, dict] = {}
    for item in items:
        entry = aggregated_items.setdefault(
            item['producto'].id,
            {
                'producto': item['producto'],
                'cantidad': 0,
            },
        )
        entry['cantidad'] += item['cantidad']

    for product_id in sorted(aggregated_items.keys()):
        item = aggregated_items[product_id]
        inventory = location_inventory_by_product[item['producto'].id]
        if inventory.stock_actual < item['cantidad']:
            raise PosSaleError(f'Stock insuficiente para {item["producto"].nombre}', status_code=409)

    for product_id in sorted(aggregated_items.keys()):
        item = aggregated_items[product_id]
        inventory = location_inventory_by_product[item['producto'].id]
        stock_anterior = inventory.stock_actual
        inventory.stock_actual = F('stock_actual') - item['cantidad']
        inventory.save(update_fields=['stock_actual', 'ultima_actualizacion'])
        inventory.refresh_from_db(fields=['stock_actual'])

        legacy_inventory = Inventario.objects.filter(producto=item['producto']).first()
        if legacy_inventory:
            legacy_stock_anterior = legacy_inventory.stock_actual
            legacy_inventory.stock_actual = F('stock_actual') - item['cantidad']
            legacy_inventory.save(update_fields=['stock_actual', 'ultima_actualizacion'])
            legacy_inventory.refresh_from_db(fields=['stock_actual'])
        else:
            legacy_stock_anterior = stock_anterior

        MovimientoInventario.objects.create(
            producto=item['producto'],
            location=location,
            organization=location.organization,
            venta=venta,
            tipo='SALIDA',
            cantidad=-item['cantidad'],
            stock_anterior=stock_anterior,
            stock_nuevo=inventory.stock_actual,
            concepto=f'Reserva venta #{venta.id}',
            registrado_por=registrado_por,
        )


def _restore_inventory(*, venta: Venta, registrado_por):
    details = list(venta.detalles.select_related('producto'))
    aggregated_items: dict[int, dict] = {}
    for detail in details:
        entry = aggregated_items.setdefault(
            detail.producto_id,
            {
                'producto': detail.producto,
                'cantidad': 0,
            },
        )
        entry['cantidad'] += detail.cantidad

    inventories = {
        inventory.producto_id: inventory
        for inventory in LocationInventory.objects.select_for_update().filter(
            location=venta.location,
            producto_id__in=aggregated_items.keys(),
        ).order_by('producto_id')
    }

    for product_id in sorted(aggregated_items.keys()):
        item = aggregated_items[product_id]
        inventory = inventories.get(item['producto'].id)
        if not inventory:
            inventory = LocationInventory.objects.create(
                location=venta.location,
                producto=item['producto'],
                stock_actual=0,
            )
            inventories[item['producto'].id] = inventory

        stock_anterior = inventory.stock_actual
        inventory.stock_actual = F('stock_actual') + item['cantidad']
        inventory.save(update_fields=['stock_actual', 'ultima_actualizacion'])
        inventory.refresh_from_db(fields=['stock_actual'])

        legacy_inventory = Inventario.objects.filter(producto=item['producto']).first()
        if legacy_inventory:
            legacy_inventory.stock_actual = F('stock_actual') + item['cantidad']
            legacy_inventory.save(update_fields=['stock_actual', 'ultima_actualizacion'])
            legacy_inventory.refresh_from_db(fields=['stock_actual'])

        MovimientoInventario.objects.create(
            producto=item['producto'],
            location=venta.location,
            organization=venta.organization,
            venta=venta,
            tipo='ENTRADA',
            cantidad=item['cantidad'],
            stock_anterior=stock_anterior,
            stock_nuevo=inventory.stock_actual,
            concepto=f'Reversion pago fallido venta #{venta.id}',
            registrado_por=registrado_por,
        )


def _process_payment(*, metodo_pago: str, total_venta: Decimal, referencia_pago: str, tarjeta_tipo: str, data: dict):
    if metodo_pago in {'EFECTIVO', 'TRANSFERENCIA'}:
        return {
            'status': 'PAID',
            'payment_provider': 'LOCAL_POS',
            'payment_reference': referencia_pago,
        }

    if data.get('force_payment_failure'):
        return {
            'status': 'FAILED',
            'reason': 'Pago de tarjeta rechazado por simulacion',
        }

    _validate_card_payment(total_venta, referencia_pago, tarjeta_tipo)
    return {
        'status': 'PAID',
        'payment_provider': data.get('payment_provider', 'POS_CARD'),
        'payment_reference': referencia_pago,
    }


def _build_inventory_snapshot_for_sale(venta: Venta) -> list[dict]:
    aggregated_items: dict[int, dict] = {}
    for detail in venta.detalles.select_related('producto'):
        entry = aggregated_items.setdefault(
            detail.producto_id,
            {
                'producto_id': detail.producto_id,
                'producto_nombre': detail.producto.nombre,
                'cantidad_vendida': 0,
            },
        )
        entry['cantidad_vendida'] += detail.cantidad

    inventory_map = {
        inventory.producto_id: inventory
        for inventory in LocationInventory.objects.filter(
            location=venta.location,
            producto_id__in=aggregated_items.keys(),
        ).only('producto_id', 'stock_actual')
    }

    snapshot = []
    for product_id in sorted(aggregated_items.keys()):
        item = aggregated_items[product_id]
        inventory = inventory_map.get(product_id)
        snapshot.append(
            {
                **item,
                'stock_actual_location': inventory.stock_actual if inventory else None,
            }
        )
    return snapshot


def _ensure_cash_turn_is_usable(turno) -> None:
    max_open_hours = max(1, int(getattr(settings, 'MAX_OPEN_CASH_TURN_HOURS', 20)))
    now = timezone.now()
    if turno.fecha_apertura and turno.fecha_apertura <= now - timedelta(hours=max_open_hours):
        raise PosSaleError(
            'La caja activa excedio el tiempo maximo permitido. Debe cerrarse y abrir un turno nuevo.',
            status_code=409,
        )

    current_operating_day = compute_operating_day(
        timestamp=now,
        timezone_name=turno.timezone_snapshot or (turno.location.timezone if turno.location_id else None),
        operating_day_ends_at=turno.operating_day_ends_at_snapshot
        or (turno.location.operating_day_ends_at if turno.location_id else None),
    )
    if turno.operating_day and current_operating_day != turno.operating_day:
        raise PosSaleError(
            'La caja activa pertenece a un dia operativo vencido. Debe cerrarse antes de seguir vendiendo.',
            status_code=409,
        )


def _get_completed_idempotent_sale(*, location_id: int, client_transaction_id: str, request_fingerprint: str):
    record = (
        IdempotencyRecord.objects.select_related('venta')
        .filter(
            location_id=location_id,
            client_transaction_id=client_transaction_id,
        )
        .first()
    )
    if not record:
        return None
    if record.request_fingerprint and record.request_fingerprint != request_fingerprint:
        raise PosSaleError('El intento reutiliza un identificador con contenido distinto', status_code=409)
    if record.status == IdempotencyRecord.Status.COMPLETED and record.venta_id:
        return record
    return None


def _build_request_fingerprint(*, location_id, cart, cart_created_at, payment_method, customer_id):
    payload = {
        'location_id': location_id,
        'cart_created_at': str(cart_created_at or ''),
        'payment_method': payment_method,
        'customer_id': customer_id,
        'cart': [
            {
                'product_id': item['producto'].id,
                'quantity': item['cantidad'],
                'unit_price': f'{item["precio_unitario"]:.2f}',
                'note': item['nota'],
            }
            for item in cart
        ],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()
    return digest


def _parse_decimal(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _build_sale_note(product_name: str, display_name: str, user_note: str) -> str:
    note = ''
    if display_name != product_name:
        note = display_name.replace(product_name, '').strip()
    if user_note:
        note = f'{note} | {user_note}' if note else user_note
    return note.strip()


def send_sale_receipt_email(venta: Venta, recipient_email: str) -> None:
    html_email = render_to_string('pos/email/factura_email.html', {'venta': venta})
    subject = f'RAMON by Bosco - Comprobante de Venta #{venta.id}'
    text_body = f'Adjunto su comprobante de venta #{venta.id} por ${venta.total}'

    if getattr(settings, 'RESEND_API_KEY', ''):
        send_resend_email(
            subject=subject,
            text_body=text_body,
            html_body=html_email,
            recipient_email=recipient_email,
        )
        return

    send_mail(
        subject=subject,
        message=text_body,
        from_email=None,
        recipient_list=[recipient_email],
        html_message=html_email,
        fail_silently=False,
    )


def send_sale_receipt_email_async(venta: Venta, recipient_email: str):
    if not recipient_email:
        return

    def send_async():
        try:
            send_sale_receipt_email(venta, recipient_email)
        except ResendEmailError:
            logger.exception('No se pudo enviar comprobante por Resend para venta #%s', venta.id)
        except Exception:
            logger.exception('No se pudo enviar comprobante por correo para venta #%s', venta.id)

    threading.Thread(target=send_async, daemon=True).start()


def _is_valid_identity(value: str) -> bool:
    return bool(value) and value.isdigit() and len(value) in (10, 13)


def _normalize_reference(value: str) -> str:
    ref = (value or '').upper().strip()
    ref = re.sub(r'\s+', '', ref)
    ref = re.sub(r'[^A-Z0-9\\-_/]', '', ref)
    return ref[:40]


def _normalize_simple_text(value: str, max_len: int) -> str:
    text = (value or '').upper().strip()
    text = re.sub(r'[^A-Z0-9 ]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text[:max_len]


def _normalize_transaction_id(value: str | None) -> str:
    text = re.sub(r'[^A-Za-z0-9\\-]', '', str(value or '').strip())
    return text[:64]


def _validate_card_payment(total_venta: Decimal, referencia_pago: str, tarjeta_tipo: str):
    if len(referencia_pago) < 6:
        raise PosSaleError('Referencia de tarjeta obligatoria (minimo 6 caracteres)', status_code=400)
    if not tarjeta_tipo:
        raise PosSaleError('Tipo de tarjeta obligatorio (credito o debito)', status_code=400)
    if tarjeta_tipo not in {'CREDITO', 'DEBITO'}:
        raise PosSaleError('Tipo de tarjeta invalido', status_code=400)

    hoy = timezone.localtime().date()
    existe_tarjeta = (
        Venta.objects.filter(
            origen='POS',
            metodo_pago='TARJETA',
            payment_reference=referencia_pago,
            total=total_venta,
            fecha__date=hoy,
            payment_status__in=[Venta.PaymentStatus.PENDING, Venta.PaymentStatus.PAID],
        )
        .exclude(estado='CANCELADO')
        .first()
    )
    if existe_tarjeta:
        raise PosSaleError(
            f'Pago con tarjeta duplicado detectado (venta #{existe_tarjeta.id})',
            status_code=400,
        )
