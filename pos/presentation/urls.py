"""Presentation routing for the POS bounded context."""

from django.urls import include, path

from . import analytics as analytics_views
from . import cash_movements as cash_movements_views
from . import cash_register as cash_register_views
from . import delivery as delivery_views
from . import inventory as inventory_views
from . import printing as printing_views
from . import staff as staff_views
from . import views as pos_views
from . import web_orders as web_orders_views

urlpatterns = [
    path('', pos_views.pos_index, name='pos_index'),
    path('privacy/', pos_views.privacy_policy, name='privacy_policy'),
    path('terms/', pos_views.terms_of_service, name='terms_of_service'),
    path('data-deletion/', pos_views.data_deletion, name='data_deletion'),
    path('registrar_venta/', pos_views.registrar_venta, name='registrar_venta'),
    path('api/transaccion-pendiente/', pos_views.consultar_transaccion_pendiente, name='api_transaccion_pendiente'),
    path('api/reconciliar-pago/', pos_views.reconciliar_pago, name='api_reconciliar_pago'),

    # Caja
    path('login/', cash_register_views.pantalla_login, name='pos_login'),
    path('api/verificar-pin/', cash_register_views.verificar_pin, name='verificar_pin'),
    path('apertura/', cash_register_views.apertura_caja, name='pos_apertura'),
    path('api/abrir-caja/', cash_register_views.abrir_caja, name='abrir_caja'),
    path('cierre/', cash_register_views.cierre_caja, name='pos_cierre'),
    path('api/procesar-cierre/', cash_register_views.procesar_cierre, name='procesar_cierre'),
    path('api/cliente/', cash_register_views.buscar_crear_cliente, name='api_cliente'),
    path('logout/', cash_register_views.cerrar_sesion, name='pos_logout'),

    # Impresion
    path('imprimir/ticket/<int:venta_id>/', printing_views.imprimir_ticket, name='imprimir_ticket'),
    path('imprimir/etiqueta-delivery/<int:venta_id>/', printing_views.imprimir_etiqueta_delivery, name='imprimir_etiqueta_delivery'),
    path('imprimir/etiqueta/<int:venta_id>/', printing_views.imprimir_etiqueta_delivery, name='imprimir_etiqueta'),
    path('imprimir/comanda/<int:venta_id>/', printing_views.imprimir_comanda, name='imprimir_comanda'),
    path('imprimir/venta/<int:venta_id>/', printing_views.imprimir_venta_completa, name='imprimir_venta_completa'),
    path('imprimir/cierre/<int:caja_id>/', printing_views.imprimir_cierre, name='imprimir_cierre'),

    # Pedidos web
    path('pedidos-web/', web_orders_views.panel_pedidos_web, name='panel_pedidos_web'),
    path('api/actualizar-pedido/', web_orders_views.api_actualizar_pedido, name='api_actualizar_pedido'),
    path('api/pedidos-web/', web_orders_views.api_pedidos_web_json, name='api_pedidos_web_json'),

    # Delivery
    path('soy-delivery/', delivery_views.delivery_portal, name='delivery_portal'),
    path('api/fijar-precio-carrera/', delivery_views.api_fijar_precio, name='api_fijar_precio'),
    path('integrations/delivery/quote/<str:token>/', delivery_views.delivery_quote_form, name='delivery_quote_form'),
    path('integrations/delivery/quote/<str:token>/submit/', delivery_views.delivery_quote_submit, name='delivery_quote_submit'),
    path('integrations/delivery/claim/<str:token>/', delivery_views.delivery_claim_form, name='delivery_claim_form'),
    path('integrations/delivery/claim/<str:token>/submit/', delivery_views.delivery_claim_submit, name='delivery_claim_submit'),
    path('integrations/delivery/in-transit/<str:token>/', delivery_views.delivery_in_transit_form, name='delivery_in_transit_form'),
    path('integrations/delivery/in-transit/<str:token>/submit/', delivery_views.delivery_in_transit_submit, name='delivery_in_transit_submit'),
    path('integrations/delivery/delivered/<str:token>/', delivery_views.delivery_delivered_form, name='delivery_delivered_form'),
    path('integrations/delivery/delivered/<str:token>/submit/', delivery_views.delivery_delivered_submit, name='delivery_delivered_submit'),

    # Integraciones
    path('', include('pos.presentation.integrations.urls')),

    # Staff
    path('empleados/', staff_views.lista_empleados, name='lista_empleados'),
    path('api/empleado/', staff_views.guardar_empleado, name='api_guardar_empleado'),
    path('api/asistencia/', staff_views.registrar_asistencia, name='api_registrar_asistencia'),

    # Movimientos de caja
    path('movimientos-caja/', cash_movements_views.panel_movimientos, name='panel_movimientos'),
    path('api/movimiento-caja/', cash_movements_views.api_registrar_movimiento, name='api_registrar_movimiento'),
    path('api/movimiento-caja/eliminar/', cash_movements_views.api_eliminar_movimiento, name='api_eliminar_movimiento'),
    path('reporte-contadora/', cash_movements_views.reporte_contadora, name='reporte_contadora'),

    # Inventario
    path('inventario/', inventory_views.panel_inventario, name='panel_inventario'),
    path('api/inventario/movimiento/', inventory_views.api_movimiento_inventario, name='api_movimiento_inventario'),
    path('api/inventario/minimo/', inventory_views.api_actualizar_minimo, name='api_actualizar_minimo'),
    path('inventario/historial/<int:producto_id>/', inventory_views.historial_inventario, name='historial_inventario'),
    path('inventario/reporte/', inventory_views.reporte_inventario_pdf, name='reporte_inventario_pdf'),

    # Analytics
    path('dashboard/', analytics_views.dashboard_analytics, name='dashboard_analytics'),
    path(
        'dashboard/offline-audited-actions/export.json',
        analytics_views.dashboard_offline_audited_actions_export_json,
        name='dashboard_offline_audited_actions_export_json',
    ),
    path(
        'dashboard/offline-audited-actions/export.csv',
        analytics_views.dashboard_offline_audited_actions_export_csv,
        name='dashboard_offline_audited_actions_export_csv',
    ),
    path(
        'dashboard/retencion-offline/',
        analytics_views.dashboard_offline_retention,
        name='dashboard_offline_retention',
    ),
    path(
        'dashboard/retencion-offline/export.json',
        analytics_views.dashboard_offline_retention_export_json,
        name='dashboard_offline_retention_export_json',
    ),
    path(
        'dashboard/retencion-offline/export.csv',
        analytics_views.dashboard_offline_retention_export_csv,
        name='dashboard_offline_retention_export_csv',
    ),
    path(
        'dashboard/retencion-offline/receipt.json',
        analytics_views.dashboard_offline_retention_receipt_json,
        name='dashboard_offline_retention_receipt_json',
    ),
    path(
        'dashboard/huerfanos-offline/',
        analytics_views.dashboard_offline_orphans,
        name='dashboard_offline_orphans',
    ),
    path(
        'dashboard/huerfanos-offline/export.json',
        analytics_views.dashboard_offline_orphans_export_json,
        name='dashboard_offline_orphans_export_json',
    ),
    path(
        'dashboard/huerfanos-offline/export.csv',
        analytics_views.dashboard_offline_orphans_export_csv,
        name='dashboard_offline_orphans_export_csv',
    ),
    path('dashboard/incidentes-offline/', analytics_views.dashboard_offline_incidents, name='dashboard_offline_incidents'),
    path(
        'dashboard/incidentes-offline/lotes/',
        analytics_views.dashboard_offline_incident_batches,
        name='dashboard_offline_incident_batches',
    ),
    path(
        'dashboard/incidentes-offline/lotes/run.json',
        analytics_views.dashboard_offline_incident_batch_json,
        name='dashboard_offline_incident_batch_json',
    ),
    path(
        'dashboard/incidentes-offline/lotes/run/',
        analytics_views.dashboard_offline_incident_batch_detail,
        name='dashboard_offline_incident_batch_detail',
    ),
    path(
        'dashboard/incidentes-offline/lotes/export.json',
        analytics_views.dashboard_offline_incident_batches_export_json,
        name='dashboard_offline_incident_batches_export_json',
    ),
    path(
        'dashboard/incidentes-offline/lotes/export.csv',
        analytics_views.dashboard_offline_incident_batches_export_csv,
        name='dashboard_offline_incident_batches_export_csv',
    ),
    path(
        'dashboard/incidentes-offline/export.json',
        analytics_views.dashboard_offline_incidents_export_json,
        name='dashboard_offline_incidents_export_json',
    ),
    path(
        'dashboard/incidentes-offline/export.csv',
        analytics_views.dashboard_offline_incidents_export_csv,
        name='dashboard_offline_incidents_export_csv',
    ),
    path(
        'dashboard/incidentes-offline/bulk/revalidate/',
        analytics_views.dashboard_offline_incidents_bulk_revalidate_json,
        name='dashboard_offline_incidents_bulk_revalidate_json',
    ),
    path(
        'dashboard/incidentes-offline/bulk/review/',
        analytics_views.dashboard_offline_incidents_bulk_review_json,
        name='dashboard_offline_incidents_bulk_review_json',
    ),
    path('dashboard/limbo-offline/', analytics_views.dashboard_offline_limbo, name='dashboard_offline_limbo'),
    path('dashboard/limbo-offline/json/', analytics_views.dashboard_offline_limbo_json, name='dashboard_offline_limbo_json'),
    path(
        'dashboard/limbo-offline/segment/',
        analytics_views.dashboard_offline_limbo_segment_detail,
        name='dashboard_offline_limbo_segment_detail',
    ),
    path(
        'dashboard/limbo-offline/segment/json/',
        analytics_views.dashboard_offline_limbo_segment_json,
        name='dashboard_offline_limbo_segment_json',
    ),
    path(
        'dashboard/limbo-offline/segment/revalidate/',
        analytics_views.dashboard_offline_limbo_segment_revalidate_json,
        name='dashboard_offline_limbo_segment_revalidate_json',
    ),
    path(
        'dashboard/limbo-offline/segment/reconcile/',
        analytics_views.dashboard_offline_limbo_segment_reconcile_json,
        name='dashboard_offline_limbo_segment_reconcile_json',
    ),
    path(
        'dashboard/limbo-offline/segment/reseal/',
        analytics_views.dashboard_offline_limbo_segment_reseal_json,
        name='dashboard_offline_limbo_segment_reseal_json',
    ),
    path(
        'dashboard/limbo-offline/segment/export-usb/',
        analytics_views.dashboard_offline_limbo_segment_export_usb_json,
        name='dashboard_offline_limbo_segment_export_usb_json',
    ),
    path(
        'dashboard/limbo-offline/segment/purge-after-usb/',
        analytics_views.dashboard_offline_limbo_segment_purge_after_usb_json,
        name='dashboard_offline_limbo_segment_purge_after_usb_json',
    ),
    path(
        'dashboard/limbo-offline/segment/review/',
        analytics_views.dashboard_offline_limbo_segment_review_json,
        name='dashboard_offline_limbo_segment_review_json',
    ),
    path(
        'dashboard/limbo-offline/reconcile/',
        analytics_views.dashboard_offline_limbo_reconcile_json,
        name='dashboard_offline_limbo_reconcile_json',
    ),
    path(
        'dashboard/limbo-offline/reseal/',
        analytics_views.dashboard_offline_limbo_reseal_json,
        name='dashboard_offline_limbo_reseal_json',
    ),
    path(
        'dashboard/limbo-offline/seal-active/',
        analytics_views.dashboard_offline_limbo_seal_json,
        name='dashboard_offline_limbo_seal_json',
    ),
    path('dashboard/resolver-excepcion-pago/', analytics_views.resolver_excepcion_pago, name='resolver_excepcion_pago'),
    path('dashboard/resolver-alerta-replay/', analytics_views.resolver_alerta_replay, name='resolver_alerta_replay'),
    path('dashboard/resolver-ajuste-contable/', analytics_views.resolver_ajuste_contable, name='resolver_ajuste_contable'),
]
