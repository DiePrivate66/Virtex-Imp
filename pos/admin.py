from django.contrib import admin

from .models import (
    Asistencia,
    CajaTurno,
    Categoria,
    Cliente,
    DeliveryQuote,
    DetalleVenta,
    Empleado,
    Inventario,
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
    list_display = ("nombre", "cedula_ruc", "telefono", "email", "fecha_registro")
    search_fields = ("nombre", "cedula_ruc", "telefono", "email")
    readonly_fields = ("fecha_registro",)


@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "icono")
    search_fields = ("nombre",)


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "categoria", "precio", "activo")
    list_filter = ("activo", "categoria")
    search_fields = ("nombre", "categoria__nombre")
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
    list_display = (
        "id",
        "fecha",
        "cliente_nombre",
        "origen",
        "tipo_pedido",
        "estado",
        "estado_pago",
        "metodo_pago",
        "total",
    )
    list_filter = ("origen", "tipo_pedido", "estado", "estado_pago", "metodo_pago", "fecha")
    search_fields = (
        "id",
        "cliente_nombre",
        "telefono_cliente",
        "telefono_cliente_e164",
        "direccion_envio",
        "referencia_pago",
    )
    readonly_fields = ("fecha",)
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
