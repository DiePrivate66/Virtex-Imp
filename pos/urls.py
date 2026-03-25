from django.urls import path
from . import views, views_caja, views_empleados, views_movimientos, views_inventario, views_analytics, views_delivery, views_integrations

urlpatterns = [
    path('', views.pos_index, name='pos_index'),
    path('registrar_venta/', views.registrar_venta, name='registrar_venta'),

    # Nuevas Rutas de Caja
    path('login/', views_caja.pantalla_login, name='pos_login'),
    path('api/verificar-pin/', views_caja.verificar_pin, name='verificar_pin'),
    path('apertura/', views_caja.apertura_caja, name='pos_apertura'),
    path('api/abrir-caja/', views_caja.abrir_caja, name='abrir_caja'),
    path('cierre/', views_caja.cierre_caja, name='pos_cierre'),
    path('api/procesar-cierre/', views_caja.procesar_cierre, name='procesar_cierre'),
    path('api/cliente/', views_caja.buscar_crear_cliente, name='api_cliente'),

    # Rutas de Impresion
    path('imprimir/ticket/<int:venta_id>/', views.imprimir_ticket, name='imprimir_ticket'),
    path('imprimir/etiqueta-delivery/<int:venta_id>/', views.imprimir_etiqueta_delivery, name='imprimir_etiqueta_delivery'),
    path('imprimir/etiqueta/<int:venta_id>/', views.imprimir_etiqueta_delivery, name='imprimir_etiqueta'),
    path('imprimir/comanda/<int:venta_id>/', views.imprimir_comanda, name='imprimir_comanda'),
    path('imprimir/venta/<int:venta_id>/', views.imprimir_venta_completa, name='imprimir_venta_completa'),
    path('imprimir/cierre/<int:caja_id>/', views.imprimir_cierre, name='imprimir_cierre'),

    # Panel de Pedidos WEB (cajero)
    path('pedidos-web/', views.panel_pedidos_web, name='panel_pedidos_web'),
    path('api/actualizar-pedido/', views.api_actualizar_pedido, name='api_actualizar_pedido'),
    path('api/pedidos-web/', views.api_pedidos_web_json, name='api_pedidos_web_json'),

    # Delivery Portal
    path('soy-delivery/', views_delivery.delivery_portal, name='delivery_portal'),
    path('api/fijar-precio-carrera/', views_delivery.api_fijar_precio, name='api_fijar_precio'),

    # Integraciones WhatsApp + Delivery Quote tokens
    path('integrations/whatsapp/webhook/', views_integrations.whatsapp_webhook, name='whatsapp_webhook'),
    path('integrations/delivery/quote/<str:token>/', views_integrations.delivery_quote_form, name='delivery_quote_form'),
    path('integrations/delivery/quote/<str:token>/submit/', views_integrations.delivery_quote_submit, name='delivery_quote_submit'),
    path('integrations/delivery/claim/<str:token>/', views_integrations.delivery_claim_form, name='delivery_claim_form'),
    path('integrations/delivery/claim/<str:token>/submit/', views_integrations.delivery_claim_submit, name='delivery_claim_submit'),
    path('api/ventas/<int:venta_id>/confirmar-whatsapp/', views_integrations.confirmar_venta_whatsapp, name='confirmar_venta_whatsapp'),

    # Print jobs
    path('api/print-jobs/pending/', views_integrations.api_print_jobs_pending, name='api_print_jobs_pending'),
    path('api/print-jobs/failed/', views_integrations.api_print_jobs_failed, name='api_print_jobs_failed'),
    path('api/print-jobs/<int:job_id>/ack/', views_integrations.api_print_job_ack, name='api_print_job_ack'),
    path('api/print-jobs/<int:job_id>/fail/', views_integrations.api_print_job_fail, name='api_print_job_fail'),
    path('api/print-jobs/<int:job_id>/retry/', views_integrations.api_print_job_retry, name='api_print_job_retry'),
    path('api/integrations/health/', views_integrations.api_integrations_health, name='api_integrations_health'),

    # Rutas de Empleados
    path('empleados/', views_empleados.lista_empleados, name='lista_empleados'),
    path('api/empleado/', views_empleados.guardar_empleado, name='api_guardar_empleado'),
    path('api/asistencia/', views_empleados.registrar_asistencia, name='api_registrar_asistencia'),

    # Movimientos de Caja (Ingresos/Gastos)
    path('movimientos-caja/', views_movimientos.panel_movimientos, name='panel_movimientos'),
    path('api/movimiento-caja/', views_movimientos.api_registrar_movimiento, name='api_registrar_movimiento'),
    path('api/movimiento-caja/eliminar/', views_movimientos.api_eliminar_movimiento, name='api_eliminar_movimiento'),
    path('reporte-contadora/', views_movimientos.reporte_contadora, name='reporte_contadora'),

    # Inventario
    path('inventario/', views_inventario.panel_inventario, name='panel_inventario'),
    path('api/inventario/movimiento/', views_inventario.api_movimiento_inventario, name='api_movimiento_inventario'),
    path('api/inventario/minimo/', views_inventario.api_actualizar_minimo, name='api_actualizar_minimo'),
    path('inventario/historial/<int:producto_id>/', views_inventario.historial_inventario, name='historial_inventario'),
    path('inventario/reporte/', views_inventario.reporte_inventario_pdf, name='reporte_inventario_pdf'),

    # Analytics Dashboard
    path('dashboard/', views_analytics.dashboard_analytics, name='dashboard_analytics'),

    # Logout
    path('logout/', views_caja.cerrar_sesion, name='pos_logout'),
]
