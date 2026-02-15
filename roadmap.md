# 🛣️ Roadmap 30 Días - Proyecto Bosco

Este es el plan de batalla para lanzar el sistema en un mes.

## Fase 1: Cimientos y POS (Semana 1) - **[EN PROGRESO]**
**Objetivo:** Tener el sistema de caja funcionando en el local.
- [x] Configuración inicial (Django, Tailwind, BD).
- [x] Carga de Menú y Colores Corporativos.
- [/] Interfaz POS (Cajero) básica.
- [ ] **Lógica de Caja:** 
    - Implementar apertura/cierre de caja (`CajaTurno`).
    - Reporte de cuadre diario.
    - Impresión de tickets (térmicos).

## Fase 2: Web App Cliente (Semana 2)
**Objetivo:** Que el cliente pueda pedir desde su celular sin descargar nada.
- [ ] **Frontend Cliente (`pedidos/`):**
    - Vista móvil del menú (similar al POS pero con fotos).
    - Carrito de compras persistente (localStorage).
    - Formulario de checkout (Nombre, Ubicación, Pago).
- [ ] **Subida de Comprobantes:**
    - Input de archivo para fotos de transferencia.
    - Guardado en servidor (`media/pagos/`).

## Fase 3: Integración y Flujo (Semana 3)
**Objetivo:** Que el pedido web suene en la caja.
- [ ] **Recepción de Pedidos WEB en POS:**
    - Polling (refresco automático) cada 30s en el dashboard del cajero.
    - Alerta visual/sonora de "Nuevo Pedido".
- [ ] **Validación de Pagos:**
    - El cajero ve la foto de la transferencia.
    - Botón "Aprobar" (pasa a Cocina) o "Rechazar".
    - Regla > $30 (Alerta especial).

## Fase 4: Despliegue y Pulido (Semana 4)
**Objetivo:** Salir a producción.
- [ ] **Infraestructura:**
    - Configurar Railway/Render.
    - Base de Datos PostgreSQL en la nube.
    - Configurar dominio (ej: `pedidos.bosco.com`).
- [ ] **Capacitación:**
    - Prueba piloto en el local (1 día).
    - Ajustes finales de interfaz.

---
**Nota para el equipo:** Mantengan este archivo actualizado marcando con `[x]` lo que completen.
