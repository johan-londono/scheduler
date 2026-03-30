# Scheduler - Sistema de Tareas Programadas

Sistema de tareas programadas con **Celery + Redis + PostgreSQL**, gestionable via **API REST (FastAPI)**. Las tareas y sus credenciales se configuran desde la base de datos sin editar código.

## Stack

- **Celery 5** — ejecución distribuida de tareas
- **Celery Beat** — programador cron
- **Redis** — broker de mensajes
- **PostgreSQL** — configuración de tareas y credenciales
- **FastAPI** — API REST para gestionar tareas

## Instalación

```bash
# 1. Entorno virtual y dependencias
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Crear .env (ver sección Variables de entorno)

# 3. Crear tablas e importar configuración inicial
python3 scripts/migrar_db.py
```

## Ejecución en desarrollo

```bash
# Celery worker + beat (en un solo proceso)
celery -A app worker --beat --loglevel=info

# API REST (puerto 8080, hot-reload)
.venv/bin/uvicorn api.main:app --reload --port 8080
```

Documentación interactiva de la API: `http://localhost:8080/docs`

## Gestión de tareas via API

### Agregar una tarea nueva

**Caso 1 — reutilizar una función existente** (distinto schedule o kwargs):
```bash
POST /tasks
{
  "name": "siigo_cliente_456",
  "function": "tasks.sincronizar_cliente_siigo.sincronizar_siigo",
  "hour": "1", "minute": "0",
  "kwargs": {"customer_id": 456, "destinatarios": ["ops@empresa.com"]},
  "credentials_id": 1
}
```
Luego `POST /system/restart` para que Beat lo tome.

**Caso 2 — función nueva** (lógica que no existe en el código):
1. Crear `tasks/mi_tarea.py` con `@celery_app.task(name="tasks.mi_tarea.mi_funcion")`
2. Agregar en `app.py`: `import tasks.mi_tarea  # noqa: F401, E402`
3. Registrar via `POST /tasks` con `"function": "tasks.mi_tarea.mi_funcion"`
4. `POST /system/restart`

> El campo `function` debe coincidir exactamente con el `name` del decorador `@celery_app.task`.
> La API controla **cuándo** (schedule/kwargs). El **cómo** (código Python) debe existir primero.

### Endpoints disponibles

| Método | Ruta | Acción |
|--------|------|--------|
| GET | `/tasks` | Lista tareas con credencial asignada |
| POST | `/tasks` | Crea tarea |
| PATCH | `/tasks/{name}` | Edita parcialmente (solo campos enviados) |
| DELETE | `/tasks/{name}` | Elimina tarea |
| POST | `/tasks/{name}/run` | Encola ejecución inmediata |
| GET | `/credentials` | Lista sets (valores sensibles enmascarados) |
| POST | `/credentials` | Crea set de credenciales |
| PATCH | `/credentials/{id}` | Fusiona vars (rota credenciales sin reescribir todo) |
| DELETE | `/credentials/{id}` | Elimina set |
| GET | `/system/status` | Tareas activas en DB |
| POST | `/system/restart` | daemon-reload + restart worker+beat |

### Ejemplos frecuentes

```bash
# Desactivar una tarea sin borrarla
PATCH /tasks/monitor_apis   {"activa": false}

# Cambiar solo el horario
PATCH /tasks/sincronizar_siigo_diario   {"hour": "2"}

# Rotar password de Dominus (afecta todas las tareas que usan ese set)
PATCH /credentials/2   {"env_vars": {"DOMINUS_ESUITE_PASSWORD": "nueva"}}

# Ejecutar una tarea ahora mismo
POST /tasks/sincronizar_siigo_diario/run
```

## Autenticación JWT

Todos los endpoints (excepto `/auth/*`) requieren un Bearer token.

### Setup inicial

```bash
# 1. Generar JWT_SECRET_KEY y agregarla al .env
python3 -c "import secrets; print(secrets.token_hex(64))"

# 2. Instalar dependencias nuevas
.venv/bin/pip install -r requirements.txt

# 3. Crear tablas de auth y primer usuario admin
python3 scripts/crear_usuarios.py --email admin@scheduler.local

# 4. Crear usuarios adicionales (opcional)
python3 scripts/crear_usuarios.py --email ops@empresa.com --rol operator
python3 scripts/crear_usuarios.py --email auditor@empresa.com --rol viewer
```

> `scripts/crear_usuarios.py` no está en el repositorio — transferirlo manualmente al servidor.

### Endpoints de auth

| Método | Ruta | Acción |
|--------|------|--------|
| POST | `/auth/login` | Email + password → `access_token` + `refresh_token` |
| POST | `/auth/refresh` | Rota el refresh token, emite uno nuevo |
| POST | `/auth/logout` | Revoca el refresh token (siempre 204) |

```bash
# Login
TOKEN=$(curl -s -X POST http://localhost:8080/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@scheduler.local","password":"..."}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Usar el token
curl http://localhost:8080/tasks -H "Authorization: Bearer $TOKEN"
```

### Roles y permisos

| Rol | Acceso |
|-----|--------|
| `viewer` | GET /tasks, GET /credentials, GET /system/status |
| `operator` | viewer + POST /tasks/{name}/run |
| `admin` | todo, incluyendo POST/PATCH/DELETE y POST /system/restart |

> La jerarquía es acumulativa: `admin` incluye todo lo de `operator` y `viewer`.

### Extensibilidad

El punto de extensión es `get_current_user()` en `api/deps.py`. Para agregar API Keys:
```python
def get_current_user(request: Request, conn=Depends(get_db)):
    if api_key := request.headers.get("X-API-Key"):
        return _autenticar_por_api_key(conn, api_key)
    # fallback a JWT...
```
Ningún router requiere cambios.

## Campos de schedule

| Campo | Valores | Ejemplo |
|-------|---------|---------|
| `minute` | `0-59`, `*`, `*/15` | `"0"` |
| `hour` | `0-23`, `*`, `*/6` | `"5"` |
| `day_of_week` | `mon,tue,...,sun`, `*` | `"mon,fri"` |
| `day_of_month` | `1-31`, `*` | `"1"` |
| `month_of_year` | `1-12`, `*` | `"*"` |

## Credenciales

Las credenciales se almacenan en la tabla `scheduler_credentials` como conjuntos de variables de entorno. Varias tareas pueden compartir el mismo set — actualizar una fila afecta todas las tareas que lo usan.

```bash
# Ver sets disponibles (passwords enmascarados)
GET /credentials

# Crear nuevo set
POST /credentials
{
  "name": "mi_api",
  "env_vars": {"API_URL": "https://...", "API_KEY": "xxx"}
}
```

## Plantillas de correo

La tarea `tasks.envio_correo.enviar_correo` acepta el kwarg `plantilla`:

| Valor | Comportamiento |
|-------|----------------|
| `null` o `"default"` | HTML dinámico con tabla de resultados |
| `"plain"` | Solo texto, sin HTML |
| `"simple"` | Carga `templates/simple.html` |
| `"nombre"` | Carga `templates/nombre.html` con `{asunto}`, `{mensaje}`, `{ahora}` |

## Producción con systemd

```bash
# Instalar los 3 servicios (worker, beat, api) en un servidor nuevo
sudo bash systemd/instalar.sh

# Tras cambios en DB, código o .env
sudo bash scripts/reiniciar.sh

# Logs
sudo journalctl -u celery-worker -f
sudo journalctl -u celery-beat -f
sudo journalctl -u celery-api -f
```

Para que `POST /system/restart` funcione, agregar a `/etc/sudoers`:
```
www-data ALL=(ALL) NOPASSWD: /bin/systemctl restart celery-worker celery-beat
www-data ALL=(ALL) NOPASSWD: /bin/systemctl daemon-reload
```

## Variables de entorno (.env)

```bash
# Redis
REDIS_URL=redis://localhost:6379/0

# DB del scheduler (tabla scheduler_tasks y scheduler_credentials)
SCHEDULER_DB_HOST=localhost
SCHEDULER_DB_PORT=5432
SCHEDULER_DB_USER=
SCHEDULER_DB_PASSWORD=
SCHEDULER_DB_DATABASE=

# DB base para scripts de tareas (sobreescribible por scheduler_credentials)
DB_HOST=
DB_PORT=5432
DB_USER=
DB_PASSWORD=
DB_DATABASE=

# Correo SMTP
MAIL_HOST=smtp.googlemail.com
MAIL_PORT=587
MAIL_USERNAME=
MAIL_PASSWORD=
MAIL_FROM_ADDRESS=
MAIL_TO=
MAIL_ENCRYPTION=tls

# API Siigo (se almacenan en scheduler_credentials tras migrar_db.py)
API_SIIGO_URL=
API_SIIGO_USER=
API_SIIGO_PASSWORD=

# API Dominus
DOMINUS_API_URL=
DOMINUS_ESUITE_USER=
DOMINUS_ESUITE_PASSWORD=
DOMINUS_CLIENT_ID=
DOMINUS_CLIENT_SECRET=
```

## Solución de problemas

**"Received unregistered task"** — La función no está registrada en el worker.
Verifica que el módulo esté importado en `app.py` y que el `name` del decorador coincida con `function` en la tabla.

**Servicios no arrancan** — Verificar que el directorio y `.env` existen en la ruta del `.service` (`/usr/share/nginx/html/scheduler`). Ver logs con `journalctl -u celery-worker -f`.

**Tarea no se ejecuta en horario** — Confirmar que Beat esté corriendo y que el timezone sea correcto (`America/Mexico_City`). Reiniciar tras cambios en la tabla.

**Error de conexión a Redis** — `redis-cli ping` debe responder `PONG`. Iniciar con `sudo systemctl start redis-server`.
