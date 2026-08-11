# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Que es este proyecto

Scheduler de tareas basado en **Celery 5 + Celery Beat + Redis**. Las tareas se configuran en PostgreSQL y se gestionan via una API FastAPI. Todo el codigo y logs estan en espanol.

## Arquitectura

```
FastAPI (api/)          → CRUD de scheduler_tasks y scheduler_credentials
    ↓ escribe en DB
PostgreSQL              → scheduler_tasks + scheduler_credentials
    ↓ relee cada 60s
Celery Beat             → encola tareas en Redis segun cron
    ↓
Celery Worker           → ejecuta la funcion Python de la tarea
    ↓
tasks/                  → que se ejecuta y cuando + reporte por correo
    ↓
etl/                    → carga de datos   (siigo in-process, dominus subprocess)
reenvio/                → reenvio de datos (DIAN, subprocess)
```

Regla de division: **`tasks/` orquesta, `etl/` y `reenvio/` trabajan.** Los modulos
de `etl/` y `reenvio/` no importan `app` ni Celery, asi que se ejecutan y se prueban
solos. Cada uno tiene su README con las convenciones para agregar features.

- **app.py** — Crea `celery_app`, expone `construir_schedule()` y `ahora()`, e importa todos los modulos de `tasks/` para registrar las funciones en el worker. **Importarlo no toca la DB**: `construir_schedule()` solo la consulta cuando Beat la llama, para que la API y los tests puedan importar el registro de tareas sin abrir Postgres.
- **beat_scheduler.py** — Scheduler custom de Beat que carga el schedule al arrancar y relee la DB cada 60s. Crear/editar/desactivar tareas cuya funcion ya existe **no requiere reiniciar**. Codigo Python nuevo si requiere reiniciar el worker. Queda declarado en `celery_app.conf.beat_scheduler`, asi que `celery -A app beat` a secas ya lo usa; el `--scheduler` del `.service` es redundante. **No quitarlo de la config**: `app.conf.beat_schedule` esta vacio a proposito, y con el scheduler por defecto Beat arrancaria sin encolar nada y sin dar error.
- **db.py** — Conexion a PostgreSQL del scheduler (`SCHEDULER_DB_*`). `obtener_tareas_activas()` hace JOIN con `scheduler_credentials` y expone `env_vars` como `env_config`.
- **api/** — FastAPI app con auth JWT. Gestiona las tablas desde HTTP. Ver endpoints abajo.
- **tasks/** — Funciones Python decoradas con `@celery_app.task`. **El nombre del modulo coincide con el del decorador**: `tasks/siigo.py` registra `tasks.siigo.*`. Cada una acepta `env_config` (dict de vars de entorno) que se inyecta al subprocess. `tasks/ejecutar.py` es el unico lanzador de subprocesos (`correr_modulo`).
- **etl/** — Carga de datos desde sistemas externos. `etl/siigo.py` se importa y se llama con `asyncio.run(run_sync(...))`; `etl/dominus.py` se ejecuta como `python -m etl.dominus`. Ver `etl/README.md`.
- **reenvio/** — Reenvio de documentos a la DIAN. Se ejecuta como `python -m reenvio.main` y devuelve el resultado en una linea `RESUMEN_JSON:`. Ver `reenvio/README.md`.
- **ops/** — Operacion: `reiniciar.sh`, `detener.sh`, `estado.sh` y `crear_usuarios.py`. Nada que las tareas importen.
- **test/** — Checks ejecutables sin framework: `python test/test_x.py`. Cada uno imprime OK o revienta con assert.

## Agregar una tarea nueva

Hay dos casos:

**La funcion ya existe** (reutilizar `sincronizar_siigo`, `enviar_correo`, etc.):
```
POST /tasks  con el name/function/schedule/kwargs deseados
```
Beat la toma sola en menos de 60s. **No hay que reiniciar nada.**

**La funcion no existe** (logica nueva):
```
1. Crear tasks/mi_tarea.py con @celery_app.task(name="tasks.mi_tarea.mi_funcion")
   (el nombre del modulo debe coincidir con el del decorador)
2. Agregar en app.py: import tasks.mi_tarea  # noqa: F401, E402
3. sudo bash ops/reiniciar.sh   — el worker necesita cargar el codigo nuevo
4. POST /tasks  con function="tasks.mi_tarea.mi_funcion"
```

> El campo `function` de la tabla debe coincidir exactamente con el `name` del decorador.
> La API gestiona el **cuando** (schedule/kwargs). El **como** (codigo) siempre debe existir en Python.
> `POST`/`PATCH /tasks` validan `function` contra el registro de Celery y `args`/`kwargs`
> contra la firma de la funcion: una tarea inejecutable se rechaza con 400 en vez de
> fallar en silencio cuando Beat la encola. `construir_schedule()` tambien ignora (con
> log de error) cualquier fila cuya funcion no este registrada.

## API FastAPI

Desarrollo: puerto **8080**. Produccion (systemd): puerto **8014**.
Documentacion interactiva en `/docs`.

Todos los endpoints salvo `/auth/*` requieren `Authorization: Bearer <access_token>`.
Roles acumulativos: `viewer` < `operator` < `admin`.

| Metodo | Ruta | Rol | Accion |
|--------|------|-----|--------|
| POST | `/auth/login` | — | email + password → access_token + refresh_token |
| POST | `/auth/refresh` | — | Rota el refresh token (revoca el anterior) |
| POST | `/auth/logout` | — | Revoca el refresh token |

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
celery -A app worker --beat --loglevel=info --scheduler=beat_scheduler:SchedulerDB

# API FastAPI (puerto 8080, con hot-reload)
.venv/bin/uvicorn api.main:app --reload --port 8080
```

### Migracion inicial (instalacion nueva)

```bash
python3 ops/migrar_db.py        # scheduler_tasks + scheduler_credentials
python3 ops/crear_usuarios.py --email admin@scheduler.local
```

> Ninguno de los dos esta versionado (`.gitignore`): se transfieren a mano al servidor.
> Si no se tienen, crear las tablas con el DDL de la seccion "Esquema de base de datos".

### Ejecutar los checks

```bash
for t in test/*.py; do PYTHONPATH=. .venv/bin/python "$t" || break; done
PYTHONPATH=. .venv/bin/python beat_scheduler.py   # autocheck del scheduler
```

### Ejecutar tareas manualmente

```bash
# Via API (recomendado)
POST /tasks/{name}/run

# Via CLI
celery -A app call tasks.monitor.verificar_apis
celery -A app call tasks.siigo.sincronizar_siigo
```

## Produccion con systemd

Tres servicios en `systemd/`: `celery-worker`, `celery-beat`, `celery-api`.
Los `.service` son plantillas con `DEPLOY_PATH` / `DEPLOY_USER` / `DEPLOY_GROUP` como placeholders.

```bash
# Instalar en servidor nuevo (sustituye placeholders y habilita servicios)
sudo bash systemd/instalar.sh --path /ruta/del/proyecto --user nombre_usuario

# Gestion diaria
sudo bash ops/reiniciar.sh   # tras cambios en DB, codigo o .env
sudo bash ops/detener.sh
bash ops/estado.sh

# Logs
sudo journalctl -u celery-worker -f
sudo journalctl -u celery-beat -f
sudo journalctl -u celery-api -f
```

## Convenciones clave

- `@celery_app.task(name="...")` — el `name` debe ser fully-qualified, coincidir con `function` en la tabla **y con la ruta del modulo**.
- Args/kwargs deben ser JSON-serializables (`accept_content=["json"]`).
- Plantillas de correo en `templates/`. El kwarg `plantilla` acepta: `null` (default HTML), `"plain"` (texto), o nombre de archivo en `templates/` (sustitucion literal de `{asunto}`, `{mensaje}`, `{ahora}`).
- Timezone: `America/Mexico_City`, definida **solo** en `app.py`. Usar `from app import ahora` en vez de `datetime.now()` para que logs, reportes y crons compartan reloj. Si la operacion pasa a horario de Colombia, cambiar `timezone` en `app.py` mueve todo a la vez.
- Una tarea que reporta por correo debe reportar **tambien cuando falla**: devolver un string de error deja la tarea en SUCCESS y el fallo invisible.
- Logica no trivial deja un check ejecutable en `test/`.
- Logs y docstrings en espanol.

## Variables de entorno (.env)

| Variable | Descripcion |
|----------|-------------|
| REDIS_URL | URL de conexion a Redis |
| JWT_SECRET_KEY | **Obligatoria**, minimo 32 caracteres. La API no arranca sin ella. Generar con `python3 -c "import secrets; print(secrets.token_hex(64))"` |
| ACCESS_TOKEN_EXPIRE_MINUTES | Vida del access token (default 30) |
| REFRESH_TOKEN_EXPIRE_DAYS | Vida del refresh token (default 7) |
| SCHEDULER_DB_HOST/PORT/USER/PASSWORD/DATABASE | DB donde viven las tablas del scheduler |
| DB_HOST/PORT/USER/PASSWORD/DATABASE | DB base para los scripts de tareas (sobreescribible via `scheduler_credentials`) |
| MAIL_HOST, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD | Configuracion SMTP |
| MAIL_FROM_ADDRESS, MAIL_TO | Remitente y destinatario por defecto |
| API_SIIGO_URL, API_SIIGO_USER, API_SIIGO_PASSWORD | Credenciales Siigo (usadas para poblar `scheduler_credentials`) |
| DOMINUS_API_URL, DOMINUS_ESUITE_USER, DOMINUS_ESUITE_PASSWORD | Credenciales Dominus |
| DOMINUS_CLIENT_ID, DOMINUS_CLIENT_SECRET | OAuth Dominus |
