# etl/ — Carga de datos

Extracción desde sistemas externos hacia las bases de datos propias. Un módulo por origen.

| Módulo | Origen | Cómo lo usa el scheduler |
|--------|--------|--------------------------|
| `siigo.py` | API Siigo | Librería: `tasks/siigo.py` la importa y llama `run_sync()` in-process |
| `dominus.py` | API Dominus vía ESuite | CLI: `python -m etl.dominus --customer N --branch N -ds F -de F -ps P` |

Ningún módulo de `etl/` importa `app` ni Celery: se ejecutan y se prueban solos.
Quién los llama y cada cuánto es problema de `tasks/`.

## Reintentos (Siigo)

La API devuelve 500 intermitentes ("Can't reconnect until invalid transaction is
rolled back"). En la página 1 son fatales: sin ella no se sabe cuántas páginas
hay y el proceso entero se cae sin traer nada. `SiigoSync._post()` reintenta ante
**5xx y errores de transporte**, con espera creciente; los 4xx se devuelven tal
cual porque no mejoran esperando.

Se ajusta desde `scheduler_credentials`, sin desplegar:

| Variable | Defecto | Qué es |
|----------|---------|--------|
| `SIIGO_INTENTOS` | 3 | Intentos totales por petición |
| `SIIGO_ESPERA_REINTENTO` | 2 | Segundos base; se multiplica por el nº de intento (2s, 4s…) |

Si un proceso agota los intentos se reporta ERROR, y las páginas adicionales que
fallen degradan el proceso a fallido aunque la primera haya funcionado.

## Agregar un origen nuevo

1. `etl/<origen>.py` con la lógica de extracción, sin dependencias del scheduler.
2. `tasks/<origen>.py` con la tarea Celery que lo invoca y reporta por correo
   (el nombre del módulo debe coincidir con el del decorador).
3. `import tasks.<origen>` en `app.py` y reiniciar el worker.
4. `POST /tasks` con `function="tasks.<origen>.<funcion>"`.

Si el origen se ejecuta como subprocess, usar `tasks.ejecutar.correr_modulo()`:
resuelve el intérprete del venv y aplica los `env_config` de las credenciales.
