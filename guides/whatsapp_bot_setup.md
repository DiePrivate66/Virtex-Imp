# WhatsApp Bot + Delivery Quote + Auto Print (Setup)

## Variables de entorno

- `PUBLIC_PWA_URL` (ej. `https://pwa.ramon.com`)
- `PUBLIC_BACKEND_URL` (ej. `https://api.ramon.com`)
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_NUMBER` (ej. `whatsapp:+14155238886`)
- `TWILIO_SIGNATURE_VALIDATION` (`True` en prod)
- `WHATSAPP_WEBHOOK_VERIFY` (token interno opcional)
- `REDIS_URL`
- `CELERY_BROKER_URL` (opcional)
- `CELERY_RESULT_BACKEND` (opcional)
- `DELIVERY_QUOTE_TIMEOUT_SECONDS` (default `180`)
- `DELIVERY_QUOTE_TOKEN_MAX_AGE_SECONDS` (default `900`)
- `WHATSAPP_INBOUND_RATE_LIMIT_WINDOW_SECONDS` (default `60`)
- `WHATSAPP_INBOUND_RATE_LIMIT_MAX` (default `20`)
- `PRINT_JOB_STUCK_SECONDS` (default `120`)

## Endpoint webhook Twilio

`POST /integrations/whatsapp/webhook/`

## Endpoint de salud operativa (cajero/admin autenticado)

`GET /api/integrations/health/`

Incluye:

- estado de configuracion Twilio;
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

- `web`: Django/Gunicorn
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

Validacion profunda Twilio (usa API remota):

```powershell
python manage.py ops_preflight --deep-twilio
```

Modo estricto (falla tambien por warnings):

```powershell
python manage.py ops_preflight --strict
```
