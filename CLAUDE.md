# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Que es este proyecto

Scheduler de tareas basado en **Celery 5 + Celery Beat + Redis**. Las tareas se configuran en una tabla PostgreSQL y se ejecutan segun cron schedules. Todo el codigo y logs estan en espanol.

## Arquitectura

**Flujo de configuracion:** tabla `scheduler_tasks` (PostgreSQL) -> `app.py` (`registrar_tareas` / `construir_crontab`) -> Celery `beat_schedule`

- **app.py** — Punto de entrada. Crea `celery_app`, lee la tabla `scheduler_tasks` via `db.obtener_tareas_activas()`, construye objetos `crontab`, y registra tareas en `beat_schedule`. Todo ocurre al importar el modulo (import-time), por lo que **cambios en la tabla requieren reiniciar el proceso**.
- **db.py** — Conexion a PostgreSQL (`obtener_conexion`) y consulta de tareas activas (`obtener_tareas_activas`). Usa las variables `DB_*` del `.env`.
- **tasks/** — Modulos de tareas organizados por dominio:
  - `tasks/siigo.py` — Sincronizacion diaria con API Siigo (invoices, customers, products, credit-notes, users)
  - `tasks/dominus.py` — Sincronizacion diaria y mensual con API Dominus (invoices, consolidated)
  - `tasks/monitor.py` — Monitoreo de estado de APIs cada 6 horas
  - `tasks/correo.py` — Envio de correos HTML con resultados de sincronizacion
  - `tasks/mantenimiento.py` — Tareas placeholder (no implementadas)
- **scripts/** — Scripts independientes ejecutados via `subprocess.run()`:
  - `scripts/siigo_script.py` — Cliente completo de API Siigo
  - `scripts/dominus_script.py` — Cliente completo de API Dominus con chunking adaptativo
  - `scripts/migrar_db.py` — Crea la tabla `scheduler_tasks` y la puebla desde `config/tasks.yaml`

## Esquema de base de datos

### scheduler_credentials
Sets de credenciales reutilizables. Cada fila es un conjunto de variables de
entorno que se inyectan al subprocess de la tarea que lo referencie.

```sql
CREATE TABLE scheduler_credentials (
    id       SERIAL PRIMARY KEY,
    name     VARCHAR(100) UNIQUE NOT NULL,  -- ej: "siigo_api", "dominus_api"
    env_vars JSONB NOT NULL DEFAULT '{}'   -- {"VAR": "valor", ...}
);
```

Sets actuales: `siigo_api` (credenciales API Siigo + DB ereports), `dominus_api`
(credenciales API Dominus/ESuite).

Para actualizar credenciales de Dominus (afecta ambas tareas automaticamente):
```sql
UPDATE scheduler_credentials
SET env_vars = env_vars || '{"DOMINUS_ESUITE_PASSWORD": "nueva"}'
WHERE name = 'dominus_api';
```

### scheduler_tasks

```sql
CREATE TABLE scheduler_tasks (
    id             SERIAL PRIMARY KEY,
    name           VARCHAR(100) UNIQUE NOT NULL,
    function       VARCHAR(200) NOT NULL,          -- fully-qualified: tasks.modulo.funcion
    minute         VARCHAR(20)  NOT NULL DEFAULT '*',
    hour           VARCHAR(20)  NOT NULL DEFAULT '*',
    day_of_week    VARCHAR(50)  NOT NULL DEFAULT '*',
    day_of_month   VARCHAR(20)  NOT NULL DEFAULT '*',
    month_of_year  VARCHAR(20)  NOT NULL DEFAULT '*',
    args           JSONB        NOT NULL DEFAULT '[]',
    kwargs         JSONB        NOT NULL DEFAULT '{}',
    credentials_id INTEGER      REFERENCES scheduler_credentials(id),  -- NULL = sin credenciales
    activa         BOOLEAN      NOT NULL DEFAULT TRUE
);
```

`db.py` hace JOIN con `scheduler_credentials` y expone `env_vars` como `env_config`
a `app.py`. Las tareas aplican `env.update(env_config)` antes de llamar al subprocess.

Para agregar o modificar tareas: editar las tablas en la DB y reiniciar los servicios.

## Tareas registradas

| Tarea | Funcion | Schedule | Descripcion |
|-------|---------|----------|-------------|
| sincronizar_siigo_diario | tasks.siigo.sincronizar_siigo | 12:00 AM diario | Sincroniza invoices, customers, products, credit-notes, users del mes actual |
| sincronizar_dominus_diario | tasks.dominus.sincronizar_dominus | 5:00 AM diario | Sincroniza invoices y consolidated del dia anterior |
| sincronizar_dominus_mensual | tasks.dominus.sincronizar_dominus_mensual | 2:00 AM dia 1 de cada mes | Sincroniza mes anterior completo con chunking adaptativo |
| monitor_apis | tasks.monitor.verificar_apis | Cada 6 horas | Verifica estado de APIs y envia correo con resultado |

## Comandos principales

### Migracion inicial (solo una vez)

```bash
# Instalacion nueva: crea ambas tablas y carga datos del .env actual
python3 scripts/migrar_db.py

# Instalacion existente con db_config: migra a env_config
python3 scripts/migrar_env_config.py

# Instalacion existente con env_config: extrae a tabla scheduler_credentials
python3 scripts/migrar_credentials.py
```

### Desarrollo

```bash
# Instalar dependencias
pip install -r requirements.txt

# Worker + beat en un solo proceso
celery -A app worker --beat --loglevel=info
```

### Ejecutar tareas manualmente

```bash
# Ejecutar una tarea especifica (la encola en Redis, requiere worker corriendo)
celery -A app call tasks.monitor.verificar_apis
celery -A app call tasks.siigo.sincronizar_siigo
celery -A app call tasks.dominus.sincronizar_dominus
celery -A app call tasks.correo.enviar_correo --kwargs='{"asunto":"Test"}'
```

> **Nota:** `celery call` solo encola la tarea en Redis. Para que se ejecute, un worker debe estar corriendo.

### Inspeccion y monitoreo

```bash
# Listar tareas registradas (requiere worker corriendo)
celery -A app inspect registered

# Tareas activas (ejecutandose ahora)
celery -A app inspect active

# Tareas reservadas (en cola del worker)
celery -A app inspect reserved

# Estadisticas del worker
celery -A app inspect stats

# Listar tareas registradas en beat_schedule (sin worker)
python3 -c "from app import celery_app; [print(f'  {k}: {v[\"task\"]}') for k,v in celery_app.conf.beat_schedule.items()]"
```

### Cola de Redis

```bash
# Ver todas las colas de Celery en Redis
redis-cli KEYS "celery*"

# Ver cuantas tareas hay pendientes en la cola
redis-cli LLEN celery

# Ver tareas en cola (sin sacarlas)
redis-cli LRANGE celery 0 -1

# Borrar todas las tareas pendientes de la cola
redis-cli DEL celery

# O usando Celery directamente
celery -A app purge -f
```

## Produccion con systemd

Los archivos `.service` estan en `systemd/` y apuntan a `/usr/share/nginx/html/scheduler` con usuario `www-data`.

### Instalar en un servidor nuevo

```bash
# Desde la raiz del proyecto (requiere root)
sudo bash systemd/instalar.sh
```

Esto copia los `.service` a `/etc/systemd/system/`, recarga systemd, habilita e inicia los servicios.

### Scripts de gestion

```bash
sudo bash scripts/reiniciar.sh   # daemon-reload + restart (usar tras cambios en tabla/codigo/.env)
sudo bash scripts/detener.sh     # stop ambos servicios
bash scripts/estado.sh           # status + lista tareas registradas en beat_schedule
```

### Comandos systemd directos

```bash
# Ver logs en tiempo real
sudo journalctl -u celery-worker -f
sudo journalctl -u celery-beat -f
```

### Cuando reiniciar los servicios

Se debe ejecutar `sudo systemctl restart celery-worker celery-beat` despues de:
- Modificar filas en `scheduler_tasks` (agregar, quitar, cambiar schedules o desactivar con `activa=FALSE`)
- Modificar archivos en `tasks/` (cambiar logica de tareas)
- Modificar `.env` (cambiar credenciales o URLs)
- Modificar `app.py` o `db.py` (cambiar configuracion de Celery o conexion)
- Actualizar `requirements.txt` e instalar nuevas dependencias

> **Importante:** La tabla se lee al iniciar el proceso. Los cambios en DB NO se recargan en caliente.

## Convenciones clave

- Al agregar una tarea: crear el archivo en `tasks/`, agregar el import en `app.py`, e insertar la fila en `scheduler_tasks`.
- El `name` del decorador `@celery_app.task(name="...")` debe coincidir exactamente con la columna `function` en la tabla.
- Args/kwargs deben ser JSON-serializables (Celery esta configurado con `accept_content=["json"]`).
- El campo `name` de la tabla es el identificador de Celery Beat y debe ser unico; `function` debe ser fully-qualified (`tasks.modulo.mi_tarea`).
- Campos cron en la tabla: `minute`, `hour`, `day_of_week`, `day_of_month`, `month_of_year`.
- Timezone: `America/Mexico_City` — no cambiar sin solicitud explicita.
- Credenciales van en variables de entorno (`.env`), nunca hardcodeadas. Redis se configura via `REDIS_URL`.
- Logs y docstrings en espanol, siguiendo el patron existente.
- No hay framework de tests ni linter configurado en este repo.

## Variables de entorno (.env)

| Variable | Descripcion |
|----------|-------------|
| REDIS_URL | URL de conexion a Redis |
| MAIL_HOST, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD | Configuracion SMTP para correos |
| MAIL_FROM_ADDRESS, MAIL_TO | Remitente y destinatario de correos |
| API_SIIGO_URL | URL base de la API Siigo |
| API_SIIGO_USER, API_SIIGO_PASSWORD | Credenciales API Siigo |
| DOMINUS_API_URL | URL base de la API Dominus (ESuite) |
| DOMINUS_ESUITE_USER, DOMINUS_ESUITE_PASSWORD | Credenciales ESuite |
| DOMINUS_CLIENT_ID, DOMINUS_CLIENT_SECRET | Credenciales OAuth Dominus |
| DOMINUS_CUSTOMER_ID, DOMINUS_BRANCH_ID | IDs de cliente y sucursal Dominus |
| SCHEDULER_DB_HOST, SCHEDULER_DB_PORT, SCHEDULER_DB_USER, SCHEDULER_DB_PASSWORD, SCHEDULER_DB_DATABASE | DB donde vive la tabla scheduler_tasks |
| DB_HOST, DB_USER, DB_PASSWORD, DB_DATABASE, DB_PORT | DB base para scripts de tareas (overrideable por db_config en la tabla) |
