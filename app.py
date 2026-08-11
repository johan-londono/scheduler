import os
import sys
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from celery import Celery
from celery.schedules import crontab

# Asegurar que el directorio del proyecto esté en sys.path.
#
# Sin condición a propósito: Celery importa este módulo dentro de cwd_in_path(),
# que mete el cwd en sys.path y lo QUITA al salir. Con un `if not in sys.path`
# el insert se saltaba (ya estaba, temporalmente) y al terminar no quedaba nada,
# así que módulos de la raíz como beat_scheduler dejaban de ser importables.
_project_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_project_dir, ".env"))
sys.path.insert(0, _project_dir)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuración de Redis
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Crear instancia de Celery
celery_app = Celery("scheduler", broker=REDIS_URL, backend=REDIS_URL)

celery_app.conf.update(
    timezone="America/Mexico_City",
    enable_utc=True,
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # El scheduler que lee scheduler_tasks va aquí, no solo en el --scheduler
    # del .service: un unit instalado antes de que existiera SchedulerDB
    # arrancaría con el scheduler por defecto y un beat_schedule vacío, sin
    # encolar nada y sin quejarse. Con esto, `celery -A app beat` a secas
    # también hace lo correcto.
    beat_scheduler="beat_scheduler:SchedulerDB",
)


def ahora():
    """Fecha y hora en la zona del scheduler.

    Un solo reloj para crons, logs y reportes: cuando cada módulo elegía la
    suya, las ejecuciones de última hora de la noche se contabilizaban en el
    día siguiente. Cambiar `timezone` arriba mueve todo a la vez.
    """
    return datetime.now(ZoneInfo(celery_app.conf.timezone))


def construir_crontab(tarea):
    """Convierte los campos de schedule de una fila de DB a un objeto crontab de Celery."""
    return crontab(
        minute=tarea.get("minute", "*"),
        hour=tarea.get("hour", "*"),
        day_of_week=tarea.get("day_of_week", "*"),
        day_of_month=tarea.get("day_of_month", "*"),
        month_of_year=tarea.get("month_of_year", "*"),
    )


def construir_schedule():
    """Lee las tareas activas de la DB y retorna el dict de beat_schedule."""
    from db import obtener_tareas_activas

    tareas = obtener_tareas_activas()
    beat_schedule = {}

    for tarea in tareas:
        nombre = tarea["name"]
        funcion = tarea["function"]

        # Encolar una función que el worker no conoce es tirar el mensaje a un
        # agujero negro: Redis lo acepta y el worker lo descarta. Mejor no
        # programarla y dejar constancia en el log.
        if funcion not in celery_app.tasks:
            logger.error(
                f"Tarea '{nombre}' ignorada: la función '{funcion}' no está "
                f"registrada en el worker."
            )
            continue

        schedule = construir_crontab(tarea)
        args = tarea.get("args") or []
        kwargs = tarea.get("kwargs") or {}

        # Si la tarea tiene env_config, lo inyecta como kwarg para que
        # el módulo de tarea lo aplique como overrides de env al subprocess.
        env_config = tarea.get("env_config")
        if env_config:
            kwargs = {**kwargs, "env_config": env_config}

        beat_schedule[nombre] = {
            "task": funcion,
            "schedule": schedule,
            "args": args,
            "kwargs": kwargs,
        }
        logger.info(f"Tarea registrada: {nombre} -> {funcion}")

    return beat_schedule


# Importar módulos de tareas para registrarlas en el worker.
# El nombre del módulo coincide con el del decorador: tasks/siigo.py registra
# "tasks.siigo.*". Si dejan de coincidir, la documentación empieza a mentir.
import tasks.siigo  # noqa: F401, E402
import tasks.correo  # noqa: F401, E402
import tasks.dominus  # noqa: F401, E402
import tasks.monitor  # noqa: F401, E402
import tasks.reenvio_dian  # noqa: F401, E402

# Importar este módulo NO toca la base de datos: construir_schedule() solo la
# consulta cuando Beat la llama (beat_scheduler.SchedulerDB). Así la API y los
# checks pueden importar el registro de tareas sin abrir Postgres.
