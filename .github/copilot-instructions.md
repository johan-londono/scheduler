# Project Guidelines

## Code Style
- Use the Python patterns already present in `app.py` and `tasks/`: small functions, module-level `logger`, f-strings for logs/returns.
- Keep task docstrings and log messages in Spanish to match the current project language.
- Define Celery tasks with explicit names that match the module path: `tasks/siigo.py` declares `@celery_app.task(name="tasks.siigo.<funcion>")`. If the two drift apart, the documentation starts lying and copy-pasted examples create tasks the worker silently discards.
- Keep task args/kwargs JSON-serializable because Celery is configured for JSON-only payloads in `app.py`.
- Use `from app import ahora` instead of `datetime.now()`: one clock for crons, logs and reports.

## Architecture
- **PostgreSQL is the source of truth for schedules** (`scheduler_tasks` + `scheduler_credentials`). There is no YAML config.
- `app.py` creates `celery_app`, exposes `construir_schedule()` / `ahora()` and imports every module under `tasks/`. Importing it must never perform I/O: the API imports it to read the task registry.
- `beat_scheduler.SchedulerDB` (`--scheduler=beat_scheduler:SchedulerDB`) loads the schedule at startup and re-reads the DB every 60s. Creating, editing or disabling a task whose Python function already exists needs no restart; new Python code does.
- `api/` is a FastAPI app with JWT auth (`api/auth.py`, roles viewer < operator < admin in `api/deps.py`).
- Work is split by domain: `tasks/` orchestrates (Celery, scheduling, email reports), `etl/` loads data in (Siigo, Dominus), `reenvio/` retries outbound documents (DIAN). **Modules under `etl/` and `reenvio/` must not import `app` or Celery** — that is what keeps them runnable and testable on their own. Each folder has a README with the convention for adding a new source or document type.
- Subprocess entry points run as `python -m <modulo>` from the project root, launched through `tasks.ejecutar.correr_modulo()` — the only place that resolves the interpreter and applies credential overrides.
- `reenvio/` reports back through a single `RESUMEN_JSON:` line on stdout. Shared logic between the three document flows lives in `reenvio/comun.py` — do not copy it into each flow again.

## Build and Test
- Install dependencies: `pip install -r requirements.txt`
- Development run (single process): `celery -A app worker --beat --loglevel=info --scheduler=beat_scheduler:SchedulerDB`
- Production-style run:
  - Worker: `celery -A app worker --loglevel=info`
  - Beat: `celery -A app beat --loglevel=info --scheduler=beat_scheduler:SchedulerDB`
  - API: `uvicorn api.main:app --port 8014`
- Checks are plain scripts, no framework: `PYTHONPATH=. python test/test_x.py`. Each prints OK or fails on an assert. Non-trivial logic should leave one behind.
- Runtime checks: `celery -A app inspect active | registered | stats`

## Project Conventions
- `scheduler_tasks.name` is unique; `function` must exactly match a registered task name. `POST`/`PATCH /tasks` validate this against the Celery registry and against the function signature.
- Cron keys: `minute`, `hour`, `day_of_week`, `day_of_month`, `month_of_year`. Watch out for `day_of_month`: setting it on a task meant to run daily silently turns it monthly.
- Credentials are sets of environment variables injected as the `env_config` kwarg. A task that gets `credentials_id` must accept that parameter.
- Preserve timezone behaviour (`America/Mexico_City`) unless explicitly requested; it is defined once in `app.py`.

## Reporting and failure modes
This project's product is a set of emails that someone trusts. Prefer a loud failure over a clean-looking report:
- A subprocess that could not do its job must exit non-zero **and** still emit its `RESUMEN_JSON` with an `error` field.
- Never return an error string from a Celery task; raise. A task in SUCCESS that sent nothing is invisible.
- When aggregating counters, distinguish deltas (sum them) from balances such as "documents out of retries" (keep the latest value).

## Security
- Do not hardcode credentials; everything comes from the environment or `scheduler_credentials`.
- `JWT_SECRET_KEY` is mandatory and validated at import: PyJWT signs HS256 with an empty key without complaining.
- Values interpolated into SQL (the `FILTRO_*` date filters) must be format-validated first; they reach the query from an API-editable table.
- Escape anything external before putting it in the HTML email body.
- Keep Celery content restrictions (`accept_content=["json"]`).
