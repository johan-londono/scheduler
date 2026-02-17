import os
import sys
import logging

import yaml
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


def cargar_configuracion():
    """Lee el archivo tasks.yaml y retorna la lista de tareas."""
    config_path = os.path.join(os.path.dirname(__file__), "config", "tasks.yaml")
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    return config.get("tasks", [])


def construir_crontab(schedule):
    """Convierte un dict de schedule YAML a un objeto crontab de Celery."""
    return crontab(
        minute=schedule.get("minute", "*"),
        hour=schedule.get("hour", "*"),
        day_of_week=schedule.get("day_of_week", "*"),
        day_of_month=schedule.get("day_of_month", "*"),
        month_of_year=schedule.get("month_of_year", "*"),
    )


def registrar_tareas():
    """Lee la configuración YAML y registra las tareas en beat_schedule."""
    tareas = cargar_configuracion()
    beat_schedule = {}

    for tarea in tareas:
        nombre = tarea["name"]
        funcion = tarea["function"]
        schedule = construir_crontab(tarea["schedule"])
        args = tarea.get("args", [])
        kwargs = tarea.get("kwargs", {})

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
