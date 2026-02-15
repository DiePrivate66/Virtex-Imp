# 📚 Guía de Instalación: PostgreSQL & Git (Para el Equipo)

## 1. Configuración de Base de Datos (PostgreSQL)

Aunque Django usa SQLite por defecto, para trabajar en equipo y preparar el despliegue es mejor usar **PostgreSQL** desde ya.

### Paso A: Instalar PostgreSQL
1.  Descarga e instala **PostgreSQL 16** desde [postgresql.org](https://www.postgresql.org/download/).
2.  Durante la instalación, te pedirá una contraseña para el usuario `postgres`. **¡NO LA OLVIDES!** (Recomendación: usa `admin` o `1234` para desarrollo local).
3.  Instala **pgAdmin 4** (viene incluido en el instalador) para ver la base de datos visualmente.

### Paso B: Instalar las librerías en Python
Abre tu terminal (con el entorno virtual activado) e instala el conector:
```bash
pip install psycopg2-binary
```

### Paso C: Crear la Base de Datos
1.  Abre `pgAdmin` o tu terminal SQL (`psql`).
2.  Ejecuta este comando SQL:
```sql
CREATE DATABASE bosco_db;
```

### Paso D: Conectar Django
(El Líder del proyecto modificará `settings.py` próximamente. Por ahora, sigan usando SQLite hasta nuevo aviso).

---

## 2. Flujo de Trabajo en Git (¡NO ROMPER EL CÓDIGO!)

Somos 4 personas tocando el mismo código. Sigan estas reglas sagradas:

### Regla de Oro: NUNCA trabajar en `main`
La rama `main` debe tener siempre código que funciona.

### Cómo empezar una tarea:
1.  **Sincronizar:** Descarga los últimos cambios de tus amigos.
    ```bash
    git checkout main
    git pull origin main
    ```
2.  **Crear Rama:** Crea tu espacio de trabajo personal.
    ```bash
    git checkout -b feature/nombre-de-tu-tarea
    # Ejemplo: git checkout -b feature/carrito-compras
    ```

### Cómo guardar cambios:
1.  Guarda tus archivos.
2.  Sube a tu rama:
    ```bash
    git add .
    git commit -m "Descripción de lo que hiciste"
    git push origin feature/nombre-de-tu-tarea
    ```

### Cómo unir tu código (Pull Request):
1.  Ve a GitHub.com.
2.  Verás un botón verde "Compare & pull request". Dale click.
3.  Escribe qué hiciste y asigna a un compañero para que lo revise.
4.  Si te aprueban, dale "Merge".

---
**¿Dudas?** Pregunten en el grupo de WhatsApp antes de ejecutar comandos destructivos.
