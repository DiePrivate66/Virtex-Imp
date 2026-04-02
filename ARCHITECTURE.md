# Arquitectura Bosco

## Objetivo

Bosco usa una arquitectura de monolito modular orientado por dominios.

La meta no es dividir el sistema en microservicios ahora, sino:

- mantener un solo backend Django desplegable
- separar responsabilidades por dominio y por capa
- hacer las views delgadas
- evitar que `pos/services.py` siga creciendo como archivo omnibus
- facilitar pruebas, cambios y onboarding

## Decision Arquitectonica

Bosco adopta:

- monolito modular
- dominios de negocio explicitos
- capa de aplicacion para casos de uso
- capa de dominio para reglas de negocio
- capa de infraestructura para integraciones externas
- capa de presentacion para views, templates y APIs

## Capas

### 1. Presentacion

Responsabilidad:

- recibir requests HTTP
- validar inputs del request
- llamar casos de uso
- devolver HTML o JSON

Incluye:

- Django views
- templates Django
- endpoints API para Angular
- tabla real de rutas HTTP en `pos/presentation/urls.py`
- tabla real de la PWA publica en `pos/presentation/api/urls.py`

No debe contener:

- reglas de negocio complejas
- transiciones de estado dispersas
- acceso a integraciones externas como logica principal

### 2. Aplicacion

Responsabilidad:

- orquestar casos de uso
- coordinar modelos, servicios y tareas async
- definir el flujo de negocio de cada accion

Ejemplos:

- crear pedido web
- aceptar pedido
- registrar venta POS
- abrir caja
- cerrar caja
- fijar costo de delivery

No debe contener:

- HTML
- respuestas HTTP
- detalles de transporte o framework

### 3. Dominio

Responsabilidad:

- modelar las reglas del negocio
- definir invariantes
- centralizar decisiones criticas

Ejemplos de reglas:

- `total_con_envio = total + costo_envio`
- una venta web a domicilio pasa primero por cotizacion
- un cajero no puede vender sin caja abierta
- un pedido listo no deberia volver a `PENDIENTE`

### 4. Infraestructura

Responsabilidad:

- integrar servicios externos y detalles tecnicos

Incluye:

- Celery
- Redis
- WhatsApp/Meta
- email
- impresion
- archivos

## Dominios de Bosco

Estos son los bounded contexts que usamos:

### Sales

- ventas POS
- detalle de venta
- pagos
- cambio
- comprobantes

### Web Orders

- pedidos de la PWA
- confirmacion del cliente
- panel de pedidos web
- transiciones de estado del pedido

### Cash Register

- login por PIN
- apertura y cierre de caja
- movimientos de caja
- cuadre

### Cash Movements

- ingresos y gastos de caja
- reporte contable operativo

### Inventory

- stock
- movimientos
- alertas
- reportes

### Staff

- empleados
- roles
- usuarios del sistema
- asistencia

### Delivery

- cotizacion de carrera
- asignacion de repartidor
- geolocalizacion
- costo de envio

### Integrations / Notifications

- WhatsApp
- correo
- mensajes al cliente
- broadcast a repartidores
- webhooks

### Printing

- tickets
- comandas
- cola de impresion

### Analytics

- dashboard
- metricas
- reportes operativos

## Estructura Objetivo

La estructura objetivo dentro de `pos` es:

```text
pos/
  application/
    analytics/
    cash_movements/
    cash_register/
    delivery/
    integrations/
    inventory/
    notifications/
    printing/
    sales/
    staff/
    web_orders/
  domain/
    shared/
    web_orders/
  infrastructure/
    delivery/
    notifications/
    printing/
    tasks/
  presentation/
    analytics/
    api/
    cash_movements/
    cash_register/
    delivery/
    integrations/
    inventory/
    printing/
    staff/
    views/
    web_orders/
    urls.py
```

## Estado Actual del Refactor

Hoy la arquitectura ya esta en migracion activa. Estado real del repo:

- `config/urls.py` ya envia la PWA publica directo a `pos.presentation.api.urls`
- `pos/urls.py` ya es una fachada de compatibilidad
- la tabla real de rutas del POS vive en `pos/presentation/urls.py`
- `pos/presentation/api` ya separa parsing de requests, respuestas HTTP y endpoint handlers; `public.py` es la fachada canonica de la PWA y `views.py` queda como compatibilidad interna
- `pedidos/views.py` y `pedidos/urls.py` ya quedaron como fachadas de compatibilidad
- `web_orders` ya tiene separadas lecturas, comandos, acciones, parsing y reglas de estado
- `domain/shared` ya concentra utilidades transversales del negocio como normalizacion de telefonos
- `sales`, `cash_register`, `cash_movements`, `inventory`, `staff`, `delivery` y `analytics` ya tienen frontera `presentation` + `application`
- `ledger_registry.py` ya actua como fuente semantica unica para cuentas de sistema, hash de registry y version fencing
- el backend ya expone activacion runtime (`LedgerRegistryActivation`), manifest generation y middleware de fencing por hash para mutaciones POS
- el POS web ya opera con idempotencia por `client_transaction_id`, outbox de eventos y reconciliacion manual de excepciones de pago
- `cash_register` y `analytics` ya incorporan flujo operativo para reembolsos pendientes, ajustes contables y alertas administrativas
- las tareas async viven en `pos/infrastructure/tasks`
- `delivery_tokens.py` y `whatsapp_utils.py` ya fueron retirados; el uso canonico vive en `pos/infrastructure/delivery`, `domain/shared` y `domain/web_orders`
- WhatsApp/Meta ya entra por `presentation.integrations`, `application.integrations` y `application.notifications`
- `pos/infrastructure/notifications/whatsapp.py` ya es fachada de compatibilidad; firma, parsing inbound, conversaciones y transporte quedaron separados en modulos propios de infraestructura
- `pos/presentation/integrations/whatsapp.py` ya es fachada de compatibilidad; webhook, confirmacion y helpers HTTP viven en modulos propios de presentation
- `pos/presentation/integrations/payloads.py` y `pos/presentation/integrations/responses.py` ya centralizan parsing JSON y respuestas HTTP de integraciones
- `pos/presentation/integrations/whatsapp_requests.py` y `pos/presentation/integrations/whatsapp_responses.py` ya encapsulan validacion/parsing y respuestas del webhook de WhatsApp
- `pos/presentation/integrations/whatsapp_endpoint.py` ya concentra el flujo HTTP del webhook; `whatsapp_webhook.py` queda como view minima con decoradores
- `pos/presentation/integrations/print_job_endpoints.py` y `pos/presentation/integrations/whatsapp_confirmation_endpoint.py` ya concentran el flujo HTTP de print jobs y confirmacion por WhatsApp; sus views quedan como wrappers minimos con decoradores
- `pos/presentation/integrations/health_endpoint.py` ya concentra el flujo HTTP del health check de integraciones y `views.py` queda como fachada canonica de presentation para ese dominio
- `pos/application/integrations/print_jobs.py` ya es fachada de compatibilidad; errores, consultas y comandos de print jobs viven en modulos dedicados
- los wrappers legacy restantes ya apuntan a fachadas de paquete (`presentation.*`, `application.*`, `infrastructure.*`) en lugar de depender de modulos internos concretos
- `pos/services.py`, `pos.views`, `pos.views_integrations`, `whatsapp_service.py`, `telegram_service.py`, `delivery_tokens.py` y `whatsapp_utils.py` ya fueron retirados; las fachadas canonicas viven en `application.notifications`, `presentation.*`, `infrastructure.delivery` y `domain/shared`
- `pos/tasks.py` ya quedo clasificado como alias operativo de Celery: es delgado, pero no se puede retirar hasta migrar nombres de tareas y beat schedule
- `pos/legacy.py` ya mantiene el mapa central de import paths legacy -> destino canonico para guiar futuras eliminaciones
- `ops_preflight` ya cubre no solo WhatsApp/printing sino tambien ledger registry, cuentas de sistema, outbox, pagos pendientes y backlog de excepciones

Zonas todavia a seguir limpiando:

- wrappers legacy que aun mantenemos por compatibilidad
- bordes de `integrations` que todavia pueden desacoplarse mas

## Riesgos Estructurales Vigentes

La migracion por dominios ya avanzo, pero todavia quedan bordes tecnicos que deben tratarse
como deuda estructural activa y no como detalles cosmeticos.

### 1. `Venta` sigue siendo un agregado demasiado cargado

Hoy `Venta` mezcla en un solo modelo:

- datos transaccionales POS
- estado de pago legacy y v2
- datos operativos de delivery
- confirmacion por cliente
- snapshots de operador y supervisor

Eso no bloquea la operacion actual, pero si vuelve mas fragiles:

- las migraciones de esquema
- las pruebas unitarias puras
- el aislamiento de reglas por dominio

Direccion de salida:

- mantener `Venta` como agregado transaccional principal
- extraer gradualmente pago y delivery a servicios o modelos relacionados
- evitar meter mas comportamiento nuevo dentro de `Venta`

### 2. Legacy y V2 de pagos aun conviven en el mismo registro

Hoy `estado_pago` y `payment_status` coexisten y se sincronizan dentro del modelo.

Eso implica:

- doble fuente de verdad temporal
- riesgo de divergencia si alguien escribe un campo sin pasar por el flujo esperado
- mayor complejidad para reporting y auditoria

Direccion de salida:

- declarar `payment_status` como campo autoritativo
- mantener `estado_pago` solo como compatibilidad de lectura o migracion
- retirar la sincronizacion dual cuando los puntos de entrada legacy desaparezcan

### 3. La multitenencia aun no es uniforme

Modelos operativos como ventas, caja, outbox, print jobs e idempotencia ya cargan
`organization` y `location`, pero catalogos como `Cliente`, `Categoria` y `Producto`
todavia no tienen frontera tenant explicita.

Eso obliga a una decision de producto y de datos:

- si el catalogo es global, debe documentarse como tal
- si Bosco va a operar como SaaS multi-tenant con catalogo propio por organizacion,
  estos modelos deben migrarse

No conviene seguir creciendo sin fijar esa decision.

### 4. Todavia existe logica de negocio relevante dentro de `save()`

Persisten invariantes y defaults criticos en modelos como `CajaTurno` y `Venta`.

Eso tiene un costo:

- dificulta razonar sobre efectos secundarios
- complica operaciones bulk
- hace menos explicitos los contratos de la capa de aplicacion

Direccion de salida:

- mover invariantes complejos a `application/` o servicios de dominio
- dejar en modelos solo validaciones minimas y consistencia local indispensable

## Prioridad de Refactor Real

El orden pragmatico actual no es "reescribir todo". Es este:

1. congelar `payment_status` como fuente autoritativa
2. decidir si `Cliente` y `Producto` son globales o tenant-scoped
3. sacar comportamiento nuevo de `save()` y de helpers de modelo
4. dividir gradualmente `Venta` en fronteras mas limpias de pago y delivery

## Mapa de Wrappers Legacy Vigentes

El registro vivo de compatibilidad se mantiene en `pos/legacy.py`.

Cada entrada documenta:

- destino canonico
- rol de compatibilidad
- fase esperada de retiro
- nota operativa
- y cada wrapper expone esa metadata via constantes de modulo (`LEGACY_MODULE_PATH`, `CANONICAL_TARGET`, `COMPATIBILITY_ROLE`, `REMOVAL_PHASE`, `LEGACY_CONTRACT`) construidas desde `pos.legacy.build_legacy_module_metadata(...)`

Ejemplos importantes:

- `pedidos.views` -> `pos.presentation.api.public`
- `pedidos.urls` -> `pos.presentation.api.urls`
- `pos.tasks` -> `pos.infrastructure.tasks` (alias operativo de Celery)

Regla operativa:

- codigo nuevo no debe importar nada desde ese mapa legacy
- wrappers solo se mantienen mientras exista algun import historico que dependa de ellos
- al eliminar uno, primero se actualiza `pos/legacy.py` y luego la documentacion
- `pedidos.views` y `pedidos.urls` ya no tienen dependencia interna viva del repo; hoy se conservan solo por compatibilidad externa y ya deben aparecer como candidatos en la auditoria
- los aliases de `phase_4` ya emiten `DeprecationWarning` al importarse para que la compatibilidad no sea silenciosa
- `phase_5` ya no tiene wrappers activos en el repo; la unica excepcion operativa permitida hoy sigue siendo `pos.tasks`

Fases de retiro:

- `phase_4_retire_legacy_entrypoints`
  Antes de borrar aliases historicos de entrada publica como `pedidos.views` o `pedidos.urls`
- `phase_5_remove_legacy_facades`
  Cuando la base interna ya no dependa de wrappers legacy y la compatibilidad externa este resuelta
- `phase_6_retire_operational_aliases`
  Cuando aliases operativos como `pos.tasks` ya no sean requeridos por nombres historicos en colas, workers, despliegues o tooling operativo

## Reglas de Diseno

### Regla 1

Las views no deben hablar directamente con demasiadas cosas a la vez.

Patron deseado:

- view -> caso de uso -> dominio/infrastructure

### Regla 2

Las transiciones de estado de `Venta` deben estar centralizadas.

No queremos reglas repartidas entre:

- templates
- views
- tasks
- utilidades sueltas

### Regla 3

Celery no decide negocio.

Celery solo ejecuta trabajo async ya decidido por la capa de aplicacion.

### Regla 4

Las integraciones externas no deben filtrarse por todo el sistema.

Ejemplos:

- Meta WhatsApp
- Google Maps links
- email
- impresion

Deben vivir detras de servicios de infraestructura.

### Regla 5

Cada nueva feature debe entrar por dominio, no por conveniencia.

Si una funcion es de `delivery`, no debe caer en un modulo general solo porque "ahi ya hay algo parecido".

## Mapa Actual -> Mapa Objetivo

### Estado actual

- la deuda legacy ya quedo concentrada en aliases de entrada publica (`pedidos.*`) y en `tasks.py`
- `integrations` todavia necesita mas desacople entre presentation, application e infrastructure
- `tasks.py` ya no se trata como wrapper legacy comun: hoy es un alias operativo de Celery y su retiro depende de migrar nombres de tareas y scheduling
- `pedidos` ya no es la entrada real de la PWA; hoy es solo compatibilidad

### Estado objetivo

- POS interno -> `presentation/views/pos.py`
- PWA pedidos -> `presentation/api/views.py` y `presentation/api/urls.py`
- casos de uso -> `application/...`
- reglas de `Venta` -> `domain/web_orders`
- Meta/WA -> `infrastructure/notifications`
- print jobs -> `infrastructure/tasks`

## Plan de Migracion

La migracion no sera big bang. Se hara por fases.

### Fase 1. Definicion

- congelar esta arquitectura como referencia oficial
- crear el esqueleto de carpetas
- dejar convenciones claras

Estado:

- completada

### Fase 2. Web Orders

Mover:

- creacion de pedido web
- panel pedidos web
- actualizacion de estado
- calculo de total con envio

Estado:

- en progreso avanzado
- panel interno ya refactorizado a `presentation` + `application` + `domain`
- la entrada publica ya vive en `presentation/api`
- `pedidos` quedo como compatibilidad

### Fase 3. Cash Register y Sales

Mover:

- login PIN
- apertura/cierre
- registrar venta
- cambio y pagos

Estado:

- en progreso
- `cash_register` y `sales` ya tienen base nueva en `application` y `presentation`

### Fase 4. Delivery, Integrations y Notifications

Mover:

- cotizaciones
- asignacion de repartidor
- WhatsApp
- timeouts

Estado:

- en progreso
- tareas async ya viven en `infrastructure/tasks`
- presentation de integraciones ya esta separada
- el enrutado de integraciones ya vive en `presentation/integrations/urls.py`
- falta seguir cortando logica legacy

### Fase 5. Inventory, Staff y Cash Movements

Mover:

- inventario
- empleados
- asistencia
- ingresos/gastos de caja

Estado:

- en progreso
- base ya creada en `presentation` + `application`
- falta reducir wrappers legacy y ordenar mas el dominio

## Criterio para Nuevas Funciones

Antes de agregar una feature nueva, responder:

1. a que dominio pertenece
2. cual es el caso de uso
3. que reglas de negocio toca
4. que integraciones externas usa
5. que capa debe cambiar

Si no podemos responder eso, no deberiamos codificar todavia.

## Decision Operativa

Bosco no va a migrar a microservicios en esta etapa.

La estrategia oficial es:

- un backend Django
- una PWA Angular
- un monolito modular bien dividido por dominios y capas

Eso nos da:

- menos complejidad operativa
- mejor velocidad de desarrollo
- menos costo de infraestructura
- mejor mantenibilidad que el enfoque actual

## Frente Actual Recomendado

El siguiente frente prioritario es seguir desacoplando:

- `integrations`
- wrappers legacy que ya no necesitemos

Razones:

- `web_orders` ya tiene bastante avance estructural
- la entrada publica ya quedo clara en `presentation/api`
- las integraciones todavia concentran decisiones sensibles entre webhook, delivery y notificaciones
- los wrappers legacy restantes todavia pueden confundir la frontera entre arquitectura nueva y compatibilidad

### Auditoria Operativa de Legacy

Para decidir si un wrapper ya puede retirarse, no vamos a depender de intuicion.
El flujo oficial es:

1. correr `python manage.py audit_legacy_imports`
2. revisar modulos marcados como `candidate`
3. confirmar que no existan dependencias externas reales pendientes
4. distinguir si el modulo es un wrapper comun o un alias operativo
5. retirar el wrapper solo en la fase indicada por `pos.legacy`

Comandos utiles:

- auditoria completa:
  `python manage.py audit_legacy_imports`
- solo candidatos de una fase:
  `python manage.py audit_legacy_imports --phase phase_5_remove_legacy_facades --candidates-only`
- salida estructurada:
  `python manage.py audit_legacy_imports --json`
- plan de retiro por fases:
  `python manage.py plan_legacy_retirement`
- plan de retiro estructurado:
  `python manage.py plan_legacy_retirement --json`
- plan de retiro filtrado por fase:
  `python manage.py plan_legacy_retirement --phase phase_5_remove_legacy_facades`
- enforcement de frontera legacy:
  `python manage.py enforce_legacy_boundaries`
- verificacion de warnings de deprecacion:
  `python manage.py verify_legacy_deprecations`

El comando distingue referencias en:

- `wrapper`
- `tests`
- `docs`
- `registry`
- `code`

`registry` corresponde al propio inventario de compatibilidad en `pos/legacy.py` y no cuenta como uso vivo.

Un wrapper solo es candidato de retiro cuando ya no tiene referencias de tipo `code`.

`plan_legacy_retirement` consume la auditoria estructurada y agrupa los candidatos reales por
fase de retiro. Eso nos da una vista operativa de:

- que wrappers ya estan listos para salir
- en que fase deberian retirarse
- que aliases operativos aun bloquean el retiro completo

La regla es:

- `audit_legacy_imports` decide si un wrapper es candidato
- `plan_legacy_retirement` decide como secuenciar el retiro
- `enforce_legacy_boundaries` protege que no reaparezcan usos vivos fuera de los aliases operativos aprobados
- `verify_legacy_deprecations` protege que los wrappers candidatos no queden silenciosos

## Regla de Oro del Proyecto

Nada nuevo entra directo a `views.py` o a un `services.py` generico si no tiene un dominio y una capa definidos.
