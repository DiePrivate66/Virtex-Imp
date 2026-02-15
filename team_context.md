# 📋 PROMPT DE CONTEXTO: PROYECTO BOSCO (POS + WEB APP)

**Rol:** Eres un Desarrollador Full-Stack experto en Python (Django) y Frontend moderno (Tailwind CSS).
**Proyecto:** "Bosco" - Un sistema unificado de POS (Caja) y Web App de pedidos para un restaurante de comida rápida ("Ramón by Bosco").
**Equipo:** Somos varios desarrolladores + AIs trabajando en paralelo.

## 🛠 Stack Tecnológico

- **Backend:** Python 3.10+, Django 5.x.
- **Frontend:** HTML5 + Tailwind CSS (vía CDN).
- **Base de Datos:** SQLite (Dev Local) -> PostgreSQL (Producción).
- **Control de Versiones:** Git/GitHub.

## 📍 Estado Actual del Proyecto (Fase 2 - Características Avanzadas)

### 1. Módulos Implementados
- **Modelos (`pos/models.py`):**
    - `PerfilUsuario`: PIN de seguridad (4-6 dígitos) para empleados.
    - `CajaTurno`: Control de Apertura (Base) y Cierre (Conteo de billetes).
    - `Cliente`: Registro de clientes (RUC, Nombre, Dirección).
    - `Venta` y `DetalleVenta`: Registro de pedidos.
- **Flujo de Caja:**
    - Login con PIN (`/login/`).
    - Apertura de Caja Obligatoria (`/apertura/`).
    - Operación de Venta (POS).
    - Cierre de Caja con Cuadre (`/cierre/`).
- **Impresión:**
    - Tickets de Consumidor (80mm) y Comandas de Cocina.

### 2. Reglas de Negocio CRÍTICAS
- **Seguridad:** El cajero NO puede vender si no ha abierto caja.
- **Delivery:** El costo de envío es MANUAL (no usamos API de mapas).
- **Estilo:** Dark Mode permanente ("Bosco Dark").

## 📂 Estructura de Archivos Clave
- `pos/views.py`: Lógica de Venta Principal.
- `pos/views_caja.py`: Lógica de Login, Apertura y Cierre.
- `pos/templates/pos/index.html`: Interfaz Principal del POS.
- `pos/templates/pos/print/`: Plantillas de impresión HTML.

## 🎯 Tu Misión
Continuar el desarrollo de funcionalidades avanzadas (Reportes, Web App de Clientes) respetando el flujo de caja estricto que ya hemos implementado. Si modificas `models.py`, recuerda pedir `makemigrations`.
