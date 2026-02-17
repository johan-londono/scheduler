# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es este proyecto

Scheduler de tareas basado en **Celery 5 + Celery Beat + Redis**. Las tareas se definen en YAML y se ejecutan según cron schedules. Todo el código y logs están en español.

## Comandos principales

```bash
# Instalar dependencias
pip install -r requirements.txt

# Desarrollo (worker + beat en un solo proceso)
celery -A app worker --beat --loglevel=info

# Producción (procesos separados)
celery -A app worker --loglevel=info   # Terminal 1
celery -A app beat --loglevel=info     # Terminal 2

# Inspección
celery -A app inspect active
celery -A app inspect registered
celery -A app inspect stats

# Ejecutar tarea manualmente
celery -A app call tasks.scripts.enviar_correo --kwargs='{"asunto":"Test"}'
```

No hay framework de tests ni linter configurado en este repo.

## Arquitectura

**Flujo de configuración:** `config/tasks.yaml` → `app.py` (registrar_tareas / construir_crontab) → Celery beat_schedule

- **app.py** — Punto de entrada. Crea `celery_app`, carga el YAML, construye objetos `crontab`, y registra tareas en `beat_schedule`. Todo ocurre al importar el módulo (import-time), por lo que **cambios en config requieren reiniciar el proceso**.
- **config/tasks.yaml** — Fuente de verdad para schedules. Cada entrada tiene `name`, `function` (path importable), `schedule` (cron fields), `args` y `kwargs`.
- **tasks/scripts.py** — Funciones de tarea decoradas con `@celery_app.task(name="tasks.scripts.<funcion>")`. El `name` del decorador debe coincidir exactamente con el `function` del YAML.

## Convenciones clave

- Al agregar una tarea: actualizar **tanto** `tasks/scripts.py` (con decorador y name explícito) **como** `config/tasks.yaml`.
- Args/kwargs deben ser JSON-serializables (Celery está configurado con `accept_content=["json"]`).
- Nombres de tarea en YAML deben ser únicos; `function` debe ser fully-qualified (`tasks.scripts.mi_tarea`).
- Campos cron del YAML: `minute`, `hour`, `day_of_week`, `day_of_month`, `month_of_year`.
- Timezone: `America/Mexico_City` — no cambiar sin solicitud explícita.
- Usar `yaml.safe_load` siempre para leer config.
- Credenciales van en variables de entorno (`.env`), nunca hardcodeadas. Redis se configura via `REDIS_URL`.
- Logs y docstrings en español, siguiendo el patrón existente.
