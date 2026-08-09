# Scheduler - Sistema de Tareas Programadas

Sistema de tareas programadas con **Celery + Redis + PostgreSQL**, gestionable via **API REST (FastAPI)**. Las tareas y sus credenciales se configuran desde la base de datos sin editar código.

## Stack

- **Celery 5** — ejecución distribuida de tareas
- **Celery Beat** — programador cron
- **Redis** — broker de mensajes
- **PostgreSQL** — configuración de tareas y credenciales
- **FastAPI** — API REST para gestionar tareas

## Estructura

```
api/        API REST: CRUD de tareas y credenciales, auth JWT
tasks/      Qué se ejecuta y cuándo. Orquesta y reporta por correo
etl/        Carga de datos desde sistemas externos (Siigo, Dominus)
reenvio/    Reenvío de documentos a la DIAN
ops/        Operación: reiniciar/detener/estado, gestión de usuarios
test/       Checks ejecutables: python test/test_x.py
```

`tasks/` orquesta; `etl/` y `reenvio/` trabajan. Los módulos de `etl/` y `reenvio/`
no importan Celery, así que se ejecutan y se prueban por separado. Cada carpeta tiene
su README con cómo agregar un origen o un tipo de documento nuevo.

## Instalación

```bash
# 1. Entorno virtual y dependencias
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo .venv/bin/pip install -r requirements.txt

# 2. Crear .env (ver sección Variables de entorno)
#    JWT_SECRET_KEY es obligatoria: la API no arranca sin ella.

# 3. Crear tablas y primer usuario
python3 ops/crear_usuarios.py --email admin@scheduler.local
```

> `ops/crear_usuarios.py` y `ops/migrar_db.py` no están versionados (contienen
> el DDL y se transfieren al servidor a mano). `crear_usuarios.py` crea `scheduler_users`
> y `scheduler_refresh_tokens`; `migrar_db.py` crea `scheduler_tasks` y
> `scheduler_credentials`. Sin ellos hay que crear las tablas con el DDL de la
> sección "Esquema de base de datos" de `CLAUDE.md`.

## Ejecución en desarrollo

```bash
# Celery worker + beat (en un solo proceso)
celery -A app worker --beat --loglevel=info --scheduler=beat_scheduler:SchedulerDB

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
  "function": "tasks.siigo.sincronizar_siigo",
  "hour": "1", "minute": "0",
  "kwargs": {"customer_id": 456, "destinatarios": ["ops@empresa.com"]},
  "credentials_id": 1
}
```
Beat la toma sola en menos de 60s. No hay que reiniciar nada.

**Caso 2 — función nueva** (lógica que no existe en el código):
1. Crear `tasks/mi_tarea.py` con `@celery_app.task(name="tasks.mi_tarea.mi_funcion")`
2. Agregar en `app.py`: `import tasks.mi_tarea  # noqa: F401, E402`
3. Registrar via `POST /tasks` con `"function": "tasks.mi_tarea.mi_funcion"`
4. `sudo bash ops/reiniciar.sh` en el servidor — el worker necesita cargar el código nuevo

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
python3 ops/crear_usuarios.py --email admin@scheduler.local

# 4. Crear usuarios adicionales (opcional)
python3 ops/crear_usuarios.py --email ops@empresa.com --rol operator
python3 ops/crear_usuarios.py --email auditor@empresa.com --rol viewer
```

> `ops/crear_usuarios.py` no está en el repositorio — transferirlo manualmente al servidor.

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
| `admin` | todo, incluyendo POST/PATCH/DELETE |

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

## Tareas disponibles

| Función | Descripción | kwargs principales |
|---------|-------------|-------------------|
| `tasks.siigo.sincronizar_siigo` | Sincroniza facturas, clientes, productos, etc. desde Siigo | `customer_id`, `procesos`, `destinatarios` |
| `tasks.dominus.sincronizar_dominus` | Sincroniza datos desde Dominus/ESuite | `env_config` |
| `tasks.correo.enviar_correo` | Envía correo con resumen | `asunto`, `mensaje`, `destinatarios`, `plantilla` |
| `tasks.monitor.verificar_apis` | Verifica el estado de las APIs externas | — |
| `tasks.reenvio_dian.reenviar_facturas_dian` | Reenvía documentos pendientes a la DIAN (máx. 3 intentos) | `key_clis`, `tipos_doc` |
| `tasks.reenvio_dian.enviar_reporte_dian_diario` | Correo consolidado del día | `destinatarios` |
| `tasks.reenvio_dian.reportar_documentos_atascados` | Documentos que agotaron los intentos, por causa | `destinatarios`, `solo_si_hay` |

### Configurar reenvío DIAN

```bash
# 1. Crear set de credenciales con los datos de conexión a la DB principal de esuite
#    y de la API de emisión. Las siete primeras son OBLIGATORIAS: sin ellas el
#    subprocess muere al importar su configuración.
POST /credentials
{
  "name": "esuite_dian",
  "env_vars": {
    "MAIN_DB_HOST": "...",
    "MAIN_DB_PORT": "5432",
    "MAIN_DB_NAME": "...",
    "MAIN_DB_USER": "...",
    "MAIN_DB_PASSWORD": "...",
    "API_PYTHON_URL": "https://...",
    "API_PYTHON_USERNAME": "...",
    "API_PYTHON_PASSWORD": "...",
    "PROVEEDOR_INTEGRACION": "AVIA",
    "MAX_INTENTOS": "3"
  }
}

# 2. Crear la tarea apuntando al set de credenciales
POST /tasks
{
  "name": "reenvio_dian_automatico",
  "function": "tasks.reenvio_dian.reenviar_facturas_dian",
  "hour": "*/2",
  "minute": "0",
  "credentials_id": <id del set esuite_dian>
}

# Para procesar un solo cliente, agregar kwargs:
POST /tasks
{
  "name": "reenvio_dian_cliente_abc",
  "function": "tasks.reenvio_dian.reenviar_facturas_dian",
  "hour": "*/1",
  "minute": "30",
  "kwargs": {"key_cli": "ABC"},
  "credentials_id": <id del set esuite_dian>
}
```

> Los archivos del servicio viven en `reenvio/` y se ejecutan como
> `python -m reenvio.main` desde la raíz del proyecto. Ver `reenvio/README.md`.

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

La tarea `tasks.correo.enviar_correo` acepta el kwarg `plantilla`:

| Valor | Comportamiento |
|-------|----------------|
| `null` o `"default"` | HTML dinámico con tabla de resultados |
| `"plain"` | Solo texto, sin HTML |
| `"simple"` | Carga `templates/simple.html` |
| `"nombre"` | Carga `templates/nombre.html` con `{asunto}`, `{mensaje}`, `{ahora}` |

## Producción con systemd

```bash
# Instalar los 3 servicios (worker, beat, api) en un servidor nuevo
# Los archivos .service son plantillas — instalar.sh sustituye los placeholders
sudo bash systemd/instalar.sh --path /ruta/del/proyecto --user nombre_usuario

# Tras cambios en DB, código o .env
sudo bash ops/reiniciar.sh

# Logs
sudo journalctl -u celery-worker -f
sudo journalctl -u celery-beat -f
sudo journalctl -u celery-api -f
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
