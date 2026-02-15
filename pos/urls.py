from django.urls import path
from . import views, views_caja, views_empleados, views_movimientos, views_inventario, views_analytics

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
    
    # Rutas de Impresión
    path('imprimir/ticket/<int:venta_id>/', views.imprimir_ticket, name='imprimir_ticket'),
    path('imprimir/comanda/<int:venta_id>/', views.imprimir_comanda, name='imprimir_comanda'),
    path('imprimir/venta/<int:venta_id>/', views.imprimir_venta_completa, name='imprimir_venta_completa'),
    path('imprimir/cierre/<int:caja_id>/', views.imprimir_cierre, name='imprimir_cierre'),

    # Panel de Pedidos WEB (cajero)
    path('pedidos-web/', views.panel_pedidos_web, name='panel_pedidos_web'),
    path('api/actualizar-pedido/', views.api_actualizar_pedido, name='api_actualizar_pedido'),
    path('api/pedidos-web/', views.api_pedidos_web_json, name='api_pedidos_web_json'),

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