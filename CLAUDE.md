# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Que es este proyecto

Scheduler de tareas basado en **Celery 5 + Celery Beat + Redis**. Las tareas se definen en YAML y se ejecutan segun cron schedules. Todo el codigo y logs estan en espanol.

## Arquitectura

**Flujo de configuracion:** `config/tasks.yaml` -> `app.py` (registrar_tareas / construir_crontab) -> Celery beat_schedule

- **app.py** — Punto de entrada. Crea `celery_app`, carga el YAML, construye objetos `crontab`, y registra tareas en `beat_schedule`. Todo ocurre al importar el modulo (import-time), por lo que **cambios en config requieren reiniciar el proceso**.
- **config/tasks.yaml** — Fuente de verdad para schedules. Cada entrada tiene `name`, `function` (path importable), `schedule` (cron fields), `args` y `kwargs`.
- **tasks/** — Modulos de tareas organizados por dominio:
  - `tasks/siigo.py` — Sincronizacion diaria con API Siigo (invoices, customers, products, credit-notes, users)
  - `tasks/dominus.py` — Sincronizacion diaria y mensual con API Dominus (invoices, consolidated)
  - `tasks/monitor.py` — Monitoreo de estado de APIs cada 10 minutos
  - `tasks/correo.py` — Envio de correos HTML con resultados de sincronizacion
  - `tasks/mantenimiento.py` — Tareas placeholder (no implementadas, comentadas en YAML)
- **scripts/** — Scripts independientes ejecutados via `subprocess.run()`:
  - `scripts/siigo_script.py` — Cliente completo de API Siigo
  - `scripts/dominus_script.py` — Cliente completo de API Dominus con chunking adaptativo

## Tareas registradas

| Tarea | Funcion | Schedule | Descripcion |
|-------|---------|----------|-------------|
| sincronizar_siigo_diario | tasks.siigo.sincronizar_siigo | 12:00 AM diario | Sincroniza invoices, customers, products, credit-notes, users del mes actual |
| sincronizar_dominus_diario | tasks.dominus.sincronizar_dominus | 5:00 AM diario | Sincroniza invoices y consolidated del dia anterior |
| sincronizar_dominus_mensual | tasks.dominus.sincronizar_dominus_mensual | 2:00 AM dia 1 de cada mes | Sincroniza mes anterior completo con chunking adaptativo |
| monitor_apis | tasks.monitor.verificar_apis | Cada 10 minutos | Verifica estado de APIs y envia correo con resultado |

## Comandos principales

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

# Listar tareas del YAML sin necesidad de worker
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

### Crear archivos de servicio

**/etc/systemd/system/celery-worker.service**
```ini
[Unit]
Description=Celery Worker - Scheduler
After=network.target redis.service

[Service]
Type=simple
User=gojofx
WorkingDirectory=/home/gojofx/projects/eholding/scheduler
EnvironmentFile=/home/gojofx/projects/eholding/scheduler/.env
ExecStart=/home/gojofx/projects/eholding/scheduler/.venv/bin/celery -A app worker --loglevel=info
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**/etc/systemd/system/celery-beat.service**
```ini
[Unit]
Description=Celery Beat - Scheduler
After=network.target redis.service celery-worker.service

[Service]
Type=simple
User=gojofx
WorkingDirectory=/home/gojofx/projects/eholding/scheduler
EnvironmentFile=/home/gojofx/projects/eholding/scheduler/.env
ExecStart=/home/gojofx/projects/eholding/scheduler/.venv/bin/celery -A app beat --loglevel=info
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Comandos systemd

```bash
# Habilitar para que inicien con el sistema
sudo systemctl enable celery-worker celery-beat

# Iniciar servicios
sudo systemctl start celery-worker celery-beat

# Ver estado
sudo systemctl status celery-worker celery-beat

# Ver logs en tiempo real
sudo journalctl -u celery-worker -f
sudo journalctl -u celery-beat -f

# Recargar configuracion de systemd (necesario si se modificaron los .service)
sudo systemctl daemon-reload

# Reiniciar (necesario despues de cambios en YAML, .env o codigo)
sudo systemctl daemon-reload
sudo systemctl restart celery-worker celery-beat

# Detener
sudo systemctl stop celery-worker celery-beat
```

### Cuando reiniciar los servicios

Se debe ejecutar `sudo systemctl restart celery-worker celery-beat` despues de:
- Modificar `config/tasks.yaml` (agregar, quitar o cambiar tareas/schedules)
- Modificar archivos en `tasks/` (cambiar logica de tareas)
- Modificar `.env` (cambiar credenciales o URLs)
- Modificar `app.py` (cambiar configuracion de Celery)
- Actualizar `requirements.txt` e instalar nuevas dependencias

> **Importante:** El YAML se lee al iniciar el proceso. Los cambios NO se recargan en caliente.

## Convenciones clave

- Al agregar una tarea: crear el archivo en `tasks/`, agregar el import en `app.py`, y registrar en `config/tasks.yaml`.
- El `name` del decorador `@celery_app.task(name="...")` debe coincidir exactamente con el `function` del YAML.
- Args/kwargs deben ser JSON-serializables (Celery esta configurado con `accept_content=["json"]`).
- Nombres de tarea en YAML deben ser unicos; `function` debe ser fully-qualified (`tasks.modulo.mi_tarea`).
- Campos cron del YAML: `minute`, `hour`, `day_of_week`, `day_of_month`, `month_of_year`.
- Timezone: `America/Mexico_City` — no cambiar sin solicitud explicita.
- Usar `yaml.safe_load` siempre para leer config.
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
| DB_HOST, DB_USER, DB_PASSWORD, DB_DATABASE, DB_PORT | Base de datos PostgreSQL ereports |
