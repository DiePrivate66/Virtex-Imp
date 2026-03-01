# Frontend Cliente (Angular PWA)

Este frontend es **solo para cliente final** (canal web de pedidos), no para caja POS.

## Stack

- Angular 21 + Service Worker (PWA)
- Django actual como API temporal (`/pedido/api/*`)

## Endpoints usados

- `GET /pedido/api/productos/`
- `POST /pedido/api/crear/`

## Desarrollo local

1. Levanta Django en el proyecto raiz:

```bash
python manage.py runserver 127.0.0.1:8000
```

2. En esta carpeta (`frontend`), ejecuta Angular con proxy hacia Django:

```bash
npm install
npm start
```

3. Abre:

```text
http://localhost:4200
```

## Build de produccion

```bash
npm run build
```

Salida:

```text
dist/frontend
```

## Despliegue Angular primero

- Publica `dist/frontend/browser` (o `dist/frontend` segun tu host) en Netlify/Vercel/Cloudflare Pages.
- Configura `src/environments/environment.prod.ts` con el backend Django publico:

```ts
apiBaseUrl: 'https://TU_BACKEND_DJANGO/pedido/api'
```

- En Django, define CORS para el dominio del frontend (variables de entorno):

```text
CORS_ALLOWED_ORIGINS=https://tu-frontend.app
CSRF_TRUSTED_ORIGINS=https://tu-frontend.app
```

- Si usas Netlify, este repo incluye `public/_redirects` para SPA routing.

## Funcionalidad incluida (v0)

- Menu por categorias
- Carrito persistente en `localStorage`
- Checkout cliente (nombre, telefono, cedula opcional)
- Tipo de pedido (`DOMICILIO` / `LLEVAR`)
- Metodo de pago (`EFECTIVO` / `TRANSFERENCIA`)
- Subida de comprobante para transferencia
- Captura GPS opcional
- Pantalla de confirmacion de pedido
