import os
import sys
import logging

from dotenv import load_dotenv
from celery import Celery
from celery.schedules import crontab

# Asegurar que el directorio del proyecto esté en sys.path
_project_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_project_dir, ".env"))
if _project_dir not in sys.path:
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
)


def construir_crontab(tarea):
    """Convierte los campos de schedule de una fila de DB a un objeto crontab de Celery."""
    return crontab(
        minute=tarea.get("minute", "*"),
        hour=tarea.get("hour", "*"),
        day_of_week=tarea.get("day_of_week", "*"),
        day_of_month=tarea.get("day_of_month", "*"),
        month_of_year=tarea.get("month_of_year", "*"),
    )


def registrar_tareas():
    """Lee las tareas activas de la base de datos y las registra en beat_schedule."""
    from db import obtener_tareas_activas

    tareas = obtener_tareas_activas()
    beat_schedule = {}

    for tarea in tareas:
        nombre = tarea["name"]
        funcion = tarea["function"]
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

    celery_app.conf.beat_schedule = beat_schedule


# Registrar tareas al importar el módulo
registrar_tareas()

# Importar módulos de tareas
import tasks.mantenimiento  # noqa: F401, E402
import tasks.siigo  # noqa: F401, E402
import tasks.correo  # noqa: F401, E402
import tasks.dominus  # noqa: F401, E402
import tasks.monitor  # noqa: F401, E402
