# Project Guidelines

## Code Style
- Use Python patterns already present in `app.py` and `tasks/scripts.py`: small functions, module-level `logger`, and f-strings for logs/returns.
- Keep task docstrings and log messages in Spanish to match current project language.
- Define Celery tasks with explicit names: `@celery_app.task(name="tasks.scripts.<funcion>")`.
- Keep task args/kwargs JSON-serializable because Celery is configured for JSON-only payloads in `app.py`.

## Architecture
- `app.py` is the scheduler wiring entrypoint: creates `celery_app`, loads `config/tasks.yaml`, builds `crontab`, and populates `beat_schedule`.
- `config/tasks.yaml` is the source of truth for schedule definitions; each `function` must point to an importable task path.
- `tasks/scripts.py` contains executable task functions; Celery autodiscovers the `tasks` package from `app.py`.
- Registration happens at import time (`registrar_tareas()`), so config changes require process restart.

## Build and Test
- Install dependencies: `pip install -r requirements.txt`
- Development run (single process): `celery -A app worker --beat --loglevel=info`
- Production-style run:
  - Worker: `celery -A app worker --loglevel=info`
  - Beat: `celery -A app beat --loglevel=info`
- Runtime checks:
  - `celery -A app inspect active`
  - `celery -A app inspect registered`
  - `celery -A app inspect stats`
- No automated test/lint commands are defined in this repo; avoid assuming `pytest`/`ruff` unless added.

## Project Conventions
- In `config/tasks.yaml`, keep `name` unique and `function` fully qualified (`tasks.scripts.mi_tarea`).
- Keep cron keys aligned with current schema: `minute`, `hour`, `day_of_week`, `day_of_month`, `month_of_year`.
- When adding a task, update both `tasks/scripts.py` and `config/tasks.yaml` so decorator name and YAML `function` match exactly.
- Preserve timezone behavior (`America/Mexico_City`) unless explicitly requested to change it.

## Integration Points
- Redis is both broker and backend via `REDIS_URL` (default `redis://localhost:6379/0`) configured in `app.py`.
- Override runtime connection via environment variable, e.g. `REDIS_URL=redis://host:6379/1 celery -A app worker --beat --loglevel=info`.
- YAML-to-Celery mapping is handled in `registrar_tareas()` and `construir_crontab()` in `app.py`.

## Security
- Keep using `yaml.safe_load` when reading task config.
- Do not hardcode Redis credentials; use `REDIS_URL` from environment.
- Validate any future filesystem/network inputs passed through task kwargs (e.g., backup destination paths).
- Keep Celery content restrictions (`accept_content=["json"]`) unless there is a strong compatibility reason to change.