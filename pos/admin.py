from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from urllib.parse import quote

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
    PerfilUsuario,
    PrintJob,
    Producto,
    Venta,
    WhatsAppConversation,
    WhatsAppMessageLog,
)


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
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Offline navigation")
    def offline_navigation_links(self, obj):
        if not obj or obj.target_model != "OfflineJournalSegment" or not obj.target_id:
            return "No aplica"
        encoded_segment_id = quote(str(obj.target_id), safe="")
        limbo_url = f'{reverse("dashboard_offline_limbo")}?segment_id={encoded_segment_id}'
        json_url = f'{reverse("dashboard_offline_limbo_segment_json")}?segment_id={encoded_segment_id}'
        return format_html(
            '<div style="display:flex;gap:8px;flex-wrap:wrap;">'
            '<a href="{}" target="_blank" rel="noopener">Abrir Limbo</a>'
            '<a href="{}" target="_blank" rel="noopener">Abrir JSON</a>'
            "</div>",
            limbo_url,
            json_url,
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
