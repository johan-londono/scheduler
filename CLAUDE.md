# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Que es este proyecto

Scheduler de tareas basado en **Celery 5 + Celery Beat + Redis**. Las tareas se configuran en PostgreSQL y se gestionan via una API FastAPI. Todo el codigo y logs estan en espanol.

## Arquitectura

```
FastAPI (api/)          → CRUD de scheduler_tasks y scheduler_credentials
    ↓ escribe en DB
PostgreSQL              → scheduler_tasks + scheduler_credentials
    ↓ se lee al arrancar
app.py                  → registra beat_schedule en memoria
    ↓
Celery Beat             → encola tareas en Redis segun cron
    ↓
Celery Worker           → ejecuta la funcion Python de la tarea
    ↓
subprocess              → siigo_script.py / dominus_script.py
```

- **app.py** — Crea `celery_app`, lee `scheduler_tasks` via `db.obtener_tareas_activas()` e importa todos los modulos de `tasks/` para registrar las funciones en el worker. **Se lee solo al arrancar** — cambios en DB requieren reiniciar.
- **db.py** — Conexion a PostgreSQL del scheduler (`SCHEDULER_DB_*`). `obtener_tareas_activas()` hace JOIN con `scheduler_credentials` y expone `env_vars` como `env_config`.
- **api/** — FastAPI app. Gestiona las tablas desde HTTP. Ver endpoints abajo.
- **tasks/** — Funciones Python decoradas con `@celery_app.task`. Cada una acepta `env_config` (dict de vars de entorno) que se inyecta al subprocess.
- **scripts/** — Scripts invocados via `subprocess.run()` desde las tareas.

## Agregar una tarea nueva

Hay dos casos:

**La funcion ya existe** (reutilizar `sincronizar_siigo`, `enviar_correo`, etc.):
```
POST /tasks  con el name/function/schedule/kwargs deseados → reiniciar servicios
```

**La funcion no existe** (logica nueva):
```
1. Crear tasks/mi_tarea.py con @celery_app.task(name="tasks.mi_tarea.mi_funcion")
2. Agregar en app.py: import tasks.mi_tarea  # noqa: F401, E402
3. POST /tasks  con function="tasks.mi_tarea.mi_funcion"
4. Reiniciar servicios
```

> El campo `function` de la tabla debe coincidir exactamente con el `name` del decorador.
> La API gestiona el **cuando** (schedule/kwargs). El **como** (codigo) siempre debe existir en Python.

## API FastAPI

Corre en el puerto **8080**. Documentacion interactiva en `/docs`.

| Metodo | Ruta | Accion |
|--------|------|--------|
| GET | `/tasks` | Lista tareas con credencial asignada |
| POST | `/tasks` | Crea tarea |
| PATCH | `/tasks/{name}` | Edita parcialmente (solo campos enviados) |
| DELETE | `/tasks/{name}` | Elimina tarea |
| POST | `/tasks/{name}/run` | Encola ejecucion inmediata en Redis |
| GET | `/credentials` | Lista sets (valores sensibles enmascarados) |
| POST | `/credentials` | Crea set de credenciales |
| PATCH | `/credentials/{id}` | Fusiona vars (no reemplaza el set completo) |
| DELETE | `/credentials/{id}` | Elimina set |
| GET | `/system/status` | Tareas activas en DB |
| POST | `/system/restart` | daemon-reload + restart worker+beat |

## Esquema de base de datos

### scheduler_credentials
```sql
CREATE TABLE scheduler_credentials (
    id       SERIAL PRIMARY KEY,
    name     VARCHAR(100) UNIQUE NOT NULL,  -- ej: "siigo_api", "dominus_api"
    env_vars JSONB NOT NULL DEFAULT '{}'   -- claves = nombre de variable de entorno
);
```
Sets actuales: `siigo_api` (API Siigo + DB ereports), `dominus_api` (API Dominus/ESuite).
Ambas tareas de Dominus comparten el mismo `credentials_id` — actualizar una fila afecta las dos.

### scheduler_tasks
```sql
CREATE TABLE scheduler_tasks (
    id             SERIAL PRIMARY KEY,
    name           VARCHAR(100) UNIQUE NOT NULL,
    function       VARCHAR(200) NOT NULL,
    minute         VARCHAR(20)  NOT NULL DEFAULT '*',
    hour           VARCHAR(20)  NOT NULL DEFAULT '*',
    day_of_week    VARCHAR(50)  NOT NULL DEFAULT '*',
    day_of_month   VARCHAR(20)  NOT NULL DEFAULT '*',
    month_of_year  VARCHAR(20)  NOT NULL DEFAULT '*',
    args           JSONB        NOT NULL DEFAULT '[]',
    kwargs         JSONB        NOT NULL DEFAULT '{}',
    credentials_id INTEGER      REFERENCES scheduler_credentials(id),
    activa         BOOLEAN      NOT NULL DEFAULT TRUE
);
```

## Comandos principales

### Desarrollo

```bash
pip install -r requirements.txt

# Celery worker + beat
celery -A app worker --beat --loglevel=info

# API FastAPI (puerto 8080, con hot-reload)
.venv/bin/uvicorn api.main:app --reload --port 8080
```

### Migracion inicial (instalacion nueva)

```bash
python3 scripts/migrar_db.py
```

### Ejecutar tareas manualmente

```bash
# Via API (recomendado)
POST /tasks/{name}/run

# Via CLI
celery -A app call tasks.monitor_estado_apis.verificar_apis
celery -A app call tasks.sincronizar_cliente_siigo.sincronizar_siigo
```

## Produccion con systemd

Tres servicios en `systemd/`: `celery-worker`, `celery-beat`, `celery-api`.
Apuntan a `/usr/share/nginx/html/scheduler` con usuario `www-data`.

```bash
# Instalar en servidor nuevo
sudo bash systemd/instalar.sh

# Gestion diaria
sudo bash scripts/reiniciar.sh   # tras cambios en DB, codigo o .env
sudo bash scripts/detener.sh
bash scripts/estado.sh

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

## Convenciones clave

- `@celery_app.task(name="...")` — el `name` debe ser fully-qualified y coincidir con `function` en la tabla.
- Args/kwargs deben ser JSON-serializables (`accept_content=["json"]`).
- Plantillas de correo en `templates/`. El kwarg `plantilla` acepta: `null` (default HTML), `"plain"` (texto), o nombre de archivo en `templates/` (interpolacion con `{asunto}`, `{mensaje}`, `{ahora}`).
- Timezone: `America/Mexico_City`.
- Logs y docstrings en espanol.

## Variables de entorno (.env)

| Variable | Descripcion |
|----------|-------------|
| REDIS_URL | URL de conexion a Redis |
| SCHEDULER_DB_HOST/PORT/USER/PASSWORD/DATABASE | DB donde viven las tablas del scheduler |
| DB_HOST/PORT/USER/PASSWORD/DATABASE | DB base para los scripts de tareas (sobreescribible via `scheduler_credentials`) |
| MAIL_HOST, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD | Configuracion SMTP |
| MAIL_FROM_ADDRESS, MAIL_TO | Remitente y destinatario por defecto |
| API_SIIGO_URL, API_SIIGO_USER, API_SIIGO_PASSWORD | Credenciales Siigo (usadas para poblar `scheduler_credentials`) |
| DOMINUS_API_URL, DOMINUS_ESUITE_USER, DOMINUS_ESUITE_PASSWORD | Credenciales Dominus |
| DOMINUS_CLIENT_ID, DOMINUS_CLIENT_SECRET | OAuth Dominus |
