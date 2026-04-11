# WhatsApp Bot + Delivery Quote + Auto Print (Meta Cloud API)

## Variables de entorno

- `PUBLIC_PWA_URL` (ej. `https://pwa.ramon.com`)
- `PUBLIC_BACKEND_URL` (ej. `https://api.ramon.com`)
- `WHATSAPP_WEBHOOK_VERIFY` (token interno opcional)

Meta Cloud API:

- `META_WHATSAPP_TOKEN`
- `META_WHATSAPP_PHONE_NUMBER_ID`
- `META_WHATSAPP_API_VERSION` (default `v22.0`)
- `META_WHATSAPP_VERIFY_TOKEN`
- `META_WHATSAPP_APP_SECRET` (opcional para firma)
- `META_SIGNATURE_VALIDATION` (`True` en prod)
- `REDIS_URL`
- `CELERY_BROKER_URL` (opcional)
- `CELERY_RESULT_BACKEND` (opcional)
- `DELIVERY_QUOTE_TIMEOUT_SECONDS` (default `180`)
- `DELIVERY_QUOTE_TOKEN_MAX_AGE_SECONDS` (default `900`)
- `WHATSAPP_INBOUND_RATE_LIMIT_WINDOW_SECONDS` (default `60`)
- `WHATSAPP_INBOUND_RATE_LIMIT_MAX` (default `20`)
- `PRINT_JOB_STUCK_SECONDS` (default `120`)
- `REPLAY_GATEWAY_ENABLED` (default `False`)
- `REPLAY_GATEWAY_TOTAL_TIMEOUT_SECONDS` (default `10`)
- `REPLAY_GATEWAY_IDLE_TIMEOUT_SECONDS` (default `5`)
- `REPLAY_GATEWAY_COLD_LANE_SLOTS` (default `2`)
- `REPLAY_GATEWAY_COLD_SLICE_SECONDS` (default `120`)

## Estado actual del backend

- el path activo de WhatsApp es Meta Cloud API unicamente;
- el transporte outbound vive en `pos/infrastructure/notifications/whatsapp_transport.py`;
- el webhook HTTP vive en `pos/presentation/integrations/whatsapp_endpoint.py`;
- `ops_preflight` valida configuracion Meta, no proveedores alternos.

Si alguien encuentra referencias viejas a Twilio o a `WHATSAPP_PROVIDER`, debe tratarlas como deuda documental o compatibilidad historica, no como contrato operativo vigente.

## Endpoint webhook Meta

- Verificacion: `GET /integrations/whatsapp/webhook/?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`
- Inbound: `POST /integrations/whatsapp/webhook/`

## Checklist de conexion real en Railway

### 1. Preparar Meta Cloud API

En Meta Developer / WhatsApp Manager:

1. crear o abrir la app de Meta usada por el restaurante;
2. habilitar producto `WhatsApp`;
3. obtener:
   - `META_WHATSAPP_TOKEN`
   - `META_WHATSAPP_PHONE_NUMBER_ID`
   - `META_WHATSAPP_APP_SECRET`
4. definir un `META_WHATSAPP_VERIFY_TOKEN` propio del entorno;
5. tener un numero de prueba o numero productivo ya vinculado.

### 2. Configurar Railway

Cargar en Railway:

- `PUBLIC_BACKEND_URL=https://<tu-backend>`
- `PUBLIC_PWA_URL=https://<tu-pwa>`
- `META_WHATSAPP_TOKEN=...`
- `META_WHATSAPP_PHONE_NUMBER_ID=...`
- `META_WHATSAPP_API_VERSION=v22.0`
- `META_WHATSAPP_VERIFY_TOKEN=...`
- `META_WHATSAPP_APP_SECRET=...`
- `META_SIGNATURE_VALIDATION=True`
- `REDIS_URL=...`
- `CELERY_BROKER_URL=...`
- `CELERY_RESULT_BACKEND=...`

Recomendado para prod:

- `DEBUG=False`
- `CELERY_TASK_ALWAYS_EAGER=False`
- `POS_REPLAY_ADMISSION_ENABLED=True`

### 3. Publicar webhook en Meta

Usar estos valores:

- Callback URL: `https://<tu-backend>/integrations/whatsapp/webhook/`
- Verify token: el mismo valor de `META_WHATSAPP_VERIFY_TOKEN`

La verificacion correcta de Meta debe devolver el `hub.challenge` en texto plano.

### 4. Suscribir eventos

En la configuracion del webhook de Meta, activar al menos:

- mensajes entrantes (`messages`)
- estados de mensajes si se van a auditar envios (`message_status` / equivalente disponible)

Si Meta no envia eventos al callback correcto, el backend no vera inbound aunque el token sea valido.

### 5. Validar preflight antes de UAT

En Railway shell o entorno equivalente:

```powershell
python manage.py ops_preflight --strict
```

El check `whatsapp_env` debe quedar en `ok/info`. Si sale warning por credenciales faltantes, no sigas con UAT.

### 6. Smoke test minimo

Hacer estas pruebas en este orden:

1. verificacion del webhook en Meta;
2. enviar un mensaje real desde un numero de prueba al numero de WhatsApp;
3. confirmar que se crea `WhatsAppMessageLog` inbound;
4. disparar una salida real desde backend y confirmar:
   - request aceptado por Graph API;
   - `WhatsAppMessageLog` outbound creado;
   - `last_outbound_at` visible en `/api/integrations/health/`.

### 7. Criterio de salida a UAT

No considerar la integracion lista si falta cualquiera de estos puntos:

- webhook verificado por Meta;
- firma habilitada en prod (`META_SIGNATURE_VALIDATION=True`);
- inbound real registrado;
- outbound real registrado;
- `ops_preflight --strict` limpio en WhatsApp/Redis/Celery.

## Checklist de debugging

Si falla la verificacion del webhook:

- revisar `META_WHATSAPP_VERIFY_TOKEN`;
- revisar que el callback URL use HTTPS publico;
- revisar que `PUBLIC_BACKEND_URL` y el dominio real coincidan.

Si llegan requests pero el backend responde `403 invalid signature`:

- revisar `META_WHATSAPP_APP_SECRET`;
- revisar `META_SIGNATURE_VALIDATION`;
- confirmar que Meta firma el request esperado del entorno actual.

Si el inbound responde `200` pero no hay efecto operativo:

- revisar `WhatsAppMessageLog`;
- revisar rate limiting (`WHATSAPP_INBOUND_RATE_LIMIT_*`);
- revisar logs del webhook y Celery.

Si el outbound queda en skipped:

- faltan `META_WHATSAPP_TOKEN` o `META_WHATSAPP_PHONE_NUMBER_ID`;
- revisar `ops_preflight`;
- revisar Railway variables efectivamente cargadas en el servicio `web` y `worker`.

Si el outbound falla contra Graph API:

- revisar token expirado o phone number id incorrecto;
- revisar permisos de la app en Meta;
- revisar el body de error guardado en `WhatsAppMessageLog.payload_json`.

## Endpoint de salud operativa (cajero/admin autenticado)

`GET /api/integrations/health/`

Incluye:

- estado de configuracion Meta;
- ultimo inbound/outbound WhatsApp;
- rate limits recientes;
- pedidos en `PENDIENTE_COTIZACION` y vencidos;
- print jobs `FAILED` y jobs trabados `IN_PROGRESS`;
- modo async (`CELERY_TASK_ALWAYS_EAGER`, broker actual).

## Flujo operativo

1. Cliente escribe por WhatsApp al numero del restaurante.
2. Webhook responde con link PWA.
3. Pedido domicilio dispara cotizacion 1:1 a roles `DELIVERY`.
4. Primer precio gana y se notifica al cliente para confirmar (SI/NO).
5. Confirmado -> estado `COCINA` + cola de impresion (`COMANDA`, `TICKET`).
6. POS en `pedidos-web` consume `/api/print-jobs/pending/` y autoimprime.
7. Celery beat ejecuta barridos cada minuto:
   - expirados de cotizacion delivery;
   - print jobs `IN_PROGRESS` trabados (reencolado).

## Procesos

Con `Procfile`:

- `web`: wrapper `scripts/start_web.py` que arranca Gunicorn directo o replay gateway + Gunicorn segun env
- `worker`: Celery worker
- `beat`: Celery beat

En local (PowerShell):

```powershell
celery -A config worker -l info
celery -A config beat -l info
```

## Preflight operativo (antes de UAT / prod)

Comando rapido:

```powershell
python manage.py ops_preflight
```

Salida JSON:

```powershell
python manage.py ops_preflight --json
```

Modo estricto (falla tambien por warnings):

```powershell
python manage.py ops_preflight --strict
```

Checks cubiertos hoy:

- database
- celery / redis
- ledger registry y version fencing
- cuentas de sistema del ledger
- ventas pendientes, idempotencia y outbox
- alertas administrativas de pagos
- WhatsApp, delivery quotes y print jobs

## Nota de compatibilidad

El backend actual opera en modo Meta-only. Variables y flujo legado de Twilio ya no forman parte del path activo de envio o recepcion.
