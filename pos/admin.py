from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from urllib.parse import quote, urlencode

from pos.application.staff.commands import sync_employee_user

from .models import (
    Asistencia,
    AuditLog,
    CajaTurno,
    Categoria,
    Cliente,
    DeliveryQuote,
    DetalleVenta,
    Empleado,
    Inventario,
    LedgerAccount,
    LedgerRegistryActivation,
    MovimientoCaja,
    MovimientoInventario,
    PendingOfflineOrphanEvent,
    PerfilUsuario,
    PrintJob,
    Producto,
    Venta,
    WhatsAppConversation,
    WhatsAppMessageLog,
)


def _truncate_admin_hint_value(raw_value, *, limit=16):
    normalized = str(raw_value or "").strip()
    if not normalized:
        return "N/A"
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


class DetalleVentaInline(admin.TabularInline):
    model = DetalleVenta
    extra = 0


@admin.register(PerfilUsuario)
class PerfilUsuarioAdmin(admin.ModelAdmin):
    list_display = ("usuario", "rol", "pin")
    list_filter = ("rol",)
    search_fields = ("usuario__username", "usuario__first_name", "usuario__last_name", "pin")


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = ("nombre", "organization", "cedula_ruc", "telefono", "email", "fecha_registro")
    list_filter = ("organization",)
    search_fields = ("nombre", "cedula_ruc", "telefono", "email", "organization__name")
    readonly_fields = ("fecha_registro",)


@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "organization", "icono")
    list_filter = ("organization",)
    search_fields = ("nombre", "organization__name")


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "organization", "categoria", "precio", "activo")
    list_filter = ("organization", "activo", "categoria")
    search_fields = ("nombre", "categoria__nombre", "organization__name")
    list_editable = ("precio", "activo")


@admin.register(CajaTurno)
class CajaTurnoAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "usuario",
        "fecha_apertura",
        "fecha_cierre",
        "base_inicial",
        "monto_final_declarado",
        "diferencia",
    )
    list_filter = ("fecha_apertura", "fecha_cierre", "usuario")
    search_fields = ("usuario__username",)
    readonly_fields = ("fecha_apertura", "fecha_cierre", "diferencia")


@admin.register(Venta)
class VentaAdmin(admin.ModelAdmin):
    @admin.display(ordering="payment_status", description="Estado pago")
    def payment_status_display(self, obj):
        return obj.get_payment_status_display()

    @admin.display(ordering="payment_reference", description="Referencia pago")
    def payment_reference_display(self, obj):
        return obj.payment_reference or "-"

    list_display = (
        "id",
        "fecha",
        "cliente_nombre",
        "origen",
        "tipo_pedido",
        "estado",
        "payment_status_display",
        "payment_method_type",
        "payment_reference_display",
        "total",
    )
    list_filter = ("origen", "tipo_pedido", "estado", "payment_status", "payment_method_type", "fecha")
    search_fields = (
        "id",
        "cliente_nombre",
        "telefono_cliente",
        "telefono_cliente_e164",
        "direccion_envio",
        "payment_reference",
    )
    readonly_fields = ("fecha", "payment_status_display", "payment_method_type", "payment_reference")
    exclude = ("estado_pago", "referencia_pago")
    inlines = [DetalleVentaInline]


@admin.register(DetalleVenta)
class DetalleVentaAdmin(admin.ModelAdmin):
    list_display = ("venta", "producto", "cantidad", "precio_unitario", "subtotal")
    list_filter = ("producto",)
    search_fields = ("venta__id", "producto__nombre", "nota")


@admin.register(Empleado)
class EmpleadoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "rol", "pin", "telefono", "activo", "usuario", "fecha_registro")
    list_filter = ("rol", "activo")
    search_fields = ("nombre", "cedula", "telefono", "pin", "usuario__username")
    list_editable = ("activo",)
    readonly_fields = ("fecha_registro",)
    actions = ("sync_selected_employee_users",)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        sync_employee_user(obj)

    @admin.action(description="Sincronizar usuarios de sistema para empleados seleccionados")
    def sync_selected_employee_users(self, request, queryset):
        synced = 0
        for empleado in queryset:
            sync_employee_user(empleado)
            synced += 1

        self.message_user(
            request,
            f"Se sincronizaron los usuarios de sistema para {synced} empleado(s).",
        )


@admin.register(Asistencia)
class AsistenciaAdmin(admin.ModelAdmin):
    list_display = ("empleado", "fecha", "hora_entrada", "hora_salida")
    list_filter = ("fecha", "empleado")
    search_fields = ("empleado__nombre",)


@admin.register(MovimientoCaja)
class MovimientoCajaAdmin(admin.ModelAdmin):
    list_display = ("turno", "tipo", "concepto", "monto", "registrado_por", "fecha")
    list_filter = ("tipo", "concepto", "fecha")
    search_fields = ("descripcion", "concepto", "registrado_por__username")
    readonly_fields = ("fecha",)


@admin.register(Inventario)
class InventarioAdmin(admin.ModelAdmin):
    list_display = ("producto", "stock_actual", "stock_minimo", "unidad", "alerta_bajo", "ultima_actualizacion")
    list_filter = ("unidad",)
    search_fields = ("producto__nombre",)
    readonly_fields = ("ultima_actualizacion", "alerta_bajo")


@admin.register(MovimientoInventario)
class MovimientoInventarioAdmin(admin.ModelAdmin):
    list_display = ("producto", "tipo", "cantidad", "stock_anterior", "stock_nuevo", "fecha")
    list_filter = ("tipo", "fecha")
    search_fields = ("producto__nombre", "concepto")
    readonly_fields = ("fecha",)


@admin.register(DeliveryQuote)
class DeliveryQuoteAdmin(admin.ModelAdmin):
    list_display = ("venta", "empleado_delivery", "precio", "estado", "created_at")
    list_filter = ("estado", "created_at")
    search_fields = ("venta__id", "empleado_delivery__nombre")
    readonly_fields = ("created_at",)


@admin.register(WhatsAppConversation)
class WhatsAppConversationAdmin(admin.ModelAdmin):
    list_display = ("telefono_e164", "estado_flujo", "venta", "last_inbound_at", "last_outbound_at")
    list_filter = ("estado_flujo",)
    search_fields = ("telefono_e164", "venta__id")


@admin.register(WhatsAppMessageLog)
class WhatsAppMessageLogAdmin(admin.ModelAdmin):
    list_display = ("telefono_e164", "direction", "status", "message_sid", "created_at")
    list_filter = ("direction", "status", "created_at")
    search_fields = ("telefono_e164", "message_sid")
    readonly_fields = ("created_at",)


@admin.register(PrintJob)
class PrintJobAdmin(admin.ModelAdmin):
    list_display = ("venta", "tipo", "estado", "reintentos", "created_at", "updated_at")
    list_filter = ("tipo", "estado", "created_at")
    search_fields = ("venta__id", "error")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "event_type",
        "organization",
        "location",
        "actor_user",
        "target_model",
        "target_id",
        "requires_attention",
    )
    list_filter = ("event_type", "organization", "location", "requires_attention", "created_at")
    search_fields = (
        "id",
        "event_type",
        "target_model",
        "target_id",
        "correlation_id",
        "actor_user__username",
        "organization__name",
        "location__name",
    )
    readonly_fields = (
        "organization",
        "location",
        "actor_user",
        "actor_staff",
        "event_type",
        "target_model",
        "target_id",
        "payload_json",
        "ip_address",
        "user_agent",
        "correlation_id",
        "requires_attention",
        "resolved_at",
        "resolved_by",
        "created_at",
        "offline_navigation_links",
        "offline_retention_receipt_summary",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Offline navigation")
    def offline_navigation_links(self, obj):
        if not obj or not obj.target_model or not obj.target_id:
            return "No aplica"
        if obj.target_model == "OfflineJournalSegment":
            encoded_segment_id = quote(str(obj.target_id), safe="")
            limbo_url = f'{reverse("dashboard_offline_limbo")}?segment_id={encoded_segment_id}'
            html_url = f'{reverse("dashboard_offline_limbo_segment_detail")}?segment_id={encoded_segment_id}'
            json_url = f'{reverse("dashboard_offline_limbo_segment_json")}?segment_id={encoded_segment_id}'
            links = [
                ('Abrir Limbo', limbo_url),
                ('Abrir HTML', html_url),
                ('Abrir JSON', json_url),
            ]
            if obj.event_type in {
                'offline.segment_usb_exported',
                'offline.segment_purged_after_usb',
            }:
                retention_params = {
                    'offline_action_segment_id': str(obj.target_id),
                    'offline_action_type': str(obj.event_type),
                }
                audit_result = str((obj.payload_json or {}).get('audit_result') or '').strip()
                if audit_result:
                    retention_params['offline_action_result'] = audit_result
                retention_url = f'{reverse("dashboard_offline_retention")}?{urlencode(retention_params)}'
                links.append(('Abrir Retencion', retention_url))
                receipt_json_url = (
                    f'{reverse("dashboard_offline_retention_receipt_json")}?'
                    f'{urlencode({"audit_log_id": obj.id})}'
                )
                links.append(('Receipt JSON', receipt_json_url))
            return format_html(
                '<div style="display:flex;gap:8px;flex-wrap:wrap;">{}</div>',
                format_html_join(
                    '',
                    '<a href="{}" target="_blank" rel="noopener">{}</a>',
                    ((href, label) for label, href in links),
                ),
            )
        if obj.target_model == "OfflineJournalSegmentBatch":
            batch_html_url = f'{reverse("dashboard_offline_incident_batch_detail")}?audit_log_id={obj.id}'
            batch_json_url = f'{reverse("dashboard_offline_incident_batch_json")}?audit_log_id={obj.id}'
            return format_html(
                '<div style="display:flex;gap:8px;flex-wrap:wrap;">'
                '<a href="{}" target="_blank" rel="noopener">Abrir Lote HTML</a>'
                '<a href="{}" target="_blank" rel="noopener">Abrir Lote JSON</a>'
                "</div>",
                batch_html_url,
                batch_json_url,
            )
        if obj.target_model == "PendingOfflineOrphanEvent":
            links = [
                (
                    'Abrir Huerfano',
                    reverse('admin:pos_pendingofflineorphanevent_change', args=[obj.target_id]),
                ),
            ]
            resolved_sale_id = str((obj.payload_json or {}).get('resolved_sale_id') or '').strip()
            if resolved_sale_id:
                links.append(
                    (
                        'Abrir Venta',
                        reverse('admin:pos_venta_change', args=[resolved_sale_id]),
                    )
                )
            return format_html(
                '<div style="display:flex;gap:8px;flex-wrap:wrap;">{}</div>',
                format_html_join(
                    '',
                    '<a href="{}" target="_blank" rel="noopener">{}</a>',
                    ((href, label) for label, href in links),
                ),
            )
        return "No aplica"

    @admin.display(description="Offline retention receipt")
    def offline_retention_receipt_summary(self, obj):
        if not obj:
            return "No aplica"
        if obj.event_type not in {
            'offline.segment_usb_exported',
            'offline.segment_purged_after_usb',
        }:
            return "No aplica"

        payload = dict(obj.payload_json or {})
        receipt_type = str(payload.get('receipt_type') or '').strip() or 'N/A'
        receipt_signature = str(payload.get('receipt_signature') or '').strip()
        truncated_receipt_signature = _truncate_admin_hint_value(receipt_signature)
        retention_reason = str(payload.get('retention_reason') or '').strip() or 'N/A'
        rows = [
            ('Receipt type', receipt_type),
            ('Audit result', str(payload.get('audit_result') or '').strip() or 'N/A'),
            ('Receipt signature', truncated_receipt_signature),
            ('Reason', retention_reason),
        ]
        if obj.event_type == 'offline.segment_usb_exported':
            usb_root = str(payload.get('usb_root') or '').strip() or 'N/A'
            rows.append(('USB root', usb_root))
            rows.append(('Retention hint', f'{receipt_type} | USB={usb_root} | sig={truncated_receipt_signature}'))
        else:
            purge_mode = str(payload.get('purge_mode') or '').strip() or 'N/A'
            manager_override = 'YES' if bool(payload.get('manager_override_confirmed')) else 'NO'
            usb_receipt_signature = _truncate_admin_hint_value(payload.get('usb_export_receipt_signature') or '')
            rows.extend(
                [
                    ('Purge mode', purge_mode),
                    ('Manager override', manager_override),
                    ('USB receipt signature', usb_receipt_signature),
                    ('Retention hint', f'{receipt_type} | MODE={purge_mode} | override={manager_override}'),
                ]
            )
        return format_html(
            '<ul style="margin:0;padding-left:18px;">{}</ul>',
            format_html_join(
                '',
                '<li><strong>{}</strong>: {}</li>',
                rows,
            ),
        )


@admin.register(PendingOfflineOrphanEvent)
class PendingOfflineOrphanEventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "status",
        "event_type",
        "organization",
        "location",
        "client_transaction_id",
        "payment_reference",
        "payment_provider",
        "resolved_sale_link",
        "resolved_at",
    )
    list_filter = ("status", "event_type", "organization", "location", "created_at")
    search_fields = (
        "id",
        "client_transaction_id",
        "payment_reference",
        "payment_provider",
        "correlation_id",
        "resolved_sale__id",
        "organization__name",
        "location__name",
    )
    readonly_fields = (
        "organization",
        "location",
        "event_type",
        "client_transaction_id",
        "payment_reference",
        "payment_provider",
        "payload_json",
        "correlation_id",
        "status",
        "resolved_sale_link",
        "resolved_at",
        "created_at",
        "related_audit_logs",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("organization", "location", "resolved_sale")
        )

    @admin.display(description="Venta resuelta")
    def resolved_sale_link(self, obj):
        if not obj or not obj.resolved_sale_id:
            return "No aplica"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">Venta #{}</a>',
            reverse('admin:pos_venta_change', args=[obj.resolved_sale_id]),
            obj.resolved_sale_id,
        )

    @admin.display(description="Audit logs relacionados")
    def related_audit_logs(self, obj):
        if not obj:
            return "No aplica"
        audits = list(
            AuditLog.objects.filter(
                target_model='PendingOfflineOrphanEvent',
                target_id=str(obj.id),
            ).order_by('-created_at')[:10]
        )
        if not audits:
            return "No hay audit logs directos"
        return format_html(
            '<ul style="margin:0;padding-left:18px;">{}</ul>',
            format_html_join(
                '',
                '<li><a href="{}" target="_blank" rel="noopener">#{}</a> · {} · {}</li>',
                (
                    (
                        reverse('admin:pos_auditlog_change', args=[audit.id]),
                        audit.id,
                        audit.event_type,
                        audit.created_at,
                    )
                    for audit in audits
                ),
            ),
        )


@admin.register(LedgerAccount)
class LedgerAccountAdmin(admin.ModelAdmin):
    list_display = ("organization", "code", "name", "account_type", "system_code", "active")
    list_filter = ("organization", "account_type", "active")
    search_fields = ("organization__name", "code", "name", "system_code")
    readonly_fields = ("created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and obj.system_code:
            readonly.extend(["organization", "code", "name", "account_type", "system_code", "active"])
        return tuple(dict.fromkeys(readonly))

    def has_delete_permission(self, request, obj=None):
        if obj and obj.system_code:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(LedgerRegistryActivation)
class LedgerRegistryActivationAdmin(admin.ModelAdmin):
    list_display = (
        "singleton_key",
        "active_registry_version",
        "active_registry_hash",
        "min_supported_queue_schema",
        "maintenance_mode",
        "activated_at",
    )
    readonly_fields = (
        "singleton_key",
        "active_registry_version",
        "active_registry_hash",
        "min_supported_queue_schema",
        "activated_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return not LedgerRegistryActivation.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
