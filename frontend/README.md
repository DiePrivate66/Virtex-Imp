# RAMON Frontend Cliente (Angular PWA)

Este frontend es solo para cliente final (canal de pedidos web/PWA).  
No reemplaza el sistema POS interno del cajero.

## Estado actual

- Angular 21 + Service Worker (PWA)
- Django como API temporal
- Integracion activa por proxy en desarrollo
- Flujo de pedido cliente funcionando
- GPS disponible solo en origen seguro (HTTPS o localhost)

## API usada por el frontend

- `GET /pedido/api/productos/`
- `POST /pedido/api/crear/`

En desarrollo, `apiBaseUrl` usa ruta relativa:

```ts
apiBaseUrl: '/pedido/api'
```

Esto permite usar `proxy.conf.json` y evita errores al abrir por tunel.

## Desarrollo local (3 terminales)

### Terminal 1: Django API

```powershell
cd D:\USER\Documents\GitHub\PROYECTO-BOSCO
& .\venv\Scripts\Activate.ps1
python manage.py runserver 127.0.0.1:8000
```

### Terminal 2: Angular

```powershell
cd D:\USER\Documents\GitHub\PROYECTO-BOSCO\frontend
npm install
npm start
```

`npm start` ya levanta:

```text
ng serve --host 0.0.0.0 --port 4200 --proxy-config proxy.conf.json
```

### Terminal 3: Tunel HTTPS (opcional para pruebas en movil)

```powershell
cd D:\USER\Documents\GitHub\PROYECTO-BOSCO\frontend
npx localtunnel --port 4200
```

Abre la URL `https://xxxx.loca.lt` en el telefono.

Si pide `Tunnel Password`, usa la IP publica:

```powershell
(Invoke-RestMethod https://api.ipify.org)
```

## Build de produccion

```powershell
npm run build
```

Salida:

```text
dist/frontend
```

## Despliegue (frontend primero)

1. Publicar `dist/frontend/browser` (o `dist/frontend` segun el host) en Netlify/Vercel/Cloudflare.
2. Configurar `src/environments/environment.prod.ts`:

```ts
apiBaseUrl: 'https://TU_BACKEND_DJANGO/pedido/api'
```

3. Configurar en Django:

```text
CORS_ALLOWED_ORIGINS=https://tu-frontend.app
CSRF_TRUSTED_ORIGINS=https://tu-frontend.app
```

## Problemas comunes

### "No se pudo cargar el menu. Verifica que Django este encendido."

Revisar:

1. Django corriendo en `127.0.0.1:8000`.
2. Angular corriendo con `npm start` (incluye proxy).
3. No usar `ng serve` sin `--proxy-config`.

### GPS no funciona en movil

El navegador solo permite geolocalizacion en:

- `https://...`
- `http://localhost`

Con IP local (`http://192.168.x.x`) puede bloquearse.

### localtunnel da 503

- El tunel se cayo o la terminal se cerro.
- Relanzar `npx localtunnel --port 4200`.
- Mantener las 3 terminales abiertas.
