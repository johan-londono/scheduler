"""Beat tiene que quedar apuntado a SchedulerDB desde la config.

    python test/test_beat_scheduler_configurado.py

Regresión real: el .service instalado en un servidor era anterior al scheduler
custom, así que corría `celery -A app beat` sin --scheduler. Mientras app.py
llenaba beat_schedule al importarse eso funcionaba de casualidad; al sacar ese
I/O del import, Beat arrancó con el schedule vacío y dejó de encolar todo sin
un solo mensaje de error. Las tareas manuales seguían funcionando, así que
parecía un problema de otra cosa.
"""

import os
import subprocess
import sys

from app import celery_app

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_scheduler_declarado_en_la_config():
    # Sin esto, `celery -A app beat` usa PersistentScheduler y lee
    # app.conf.beat_schedule, que está vacío a propósito.
    assert celery_app.conf.beat_scheduler == "beat_scheduler:SchedulerDB", (
        f"beat_scheduler apunta a {celery_app.conf.beat_scheduler!r}; Beat no leería la DB"
    )
    print("OK beat_scheduler en la config")


def test_raiz_sigue_en_sys_path_tras_cwd_in_path():
    """La raíz del proyecto tiene que sobrevivir a cwd_in_path().

    Celery importa `app` dentro de cwd_in_path(), que mete el cwd en sys.path y
    lo quita al salir. Si app.py inserta su directorio solo `if not in sys.path`,
    se lo salta (ya estaba) y al salir no queda nada: beat muere con
    ModuleNotFoundError al resolver el scheduler por nombre.

    Subproceso sin PYTHONPATH y con -P (no antepone el cwd a sys.path): así es
    como corre el script `celery`, cuyo sys.path[0] es .venv/bin. Sin -P, python
    mete el cwd por su cuenta y el check pasaría aunque el bug estuviera vivo.
    """
    codigo = (
        "from celery.utils.imports import cwd_in_path\n"
        "with cwd_in_path():\n"
        "    import app\n"          # como hace Celery al resolver -A app
        "import beat_scheduler\n"   # lo que falla si la raíz se perdió
        "print('IMPORTABLE')\n"
    )
    entorno = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-P", "-c", codigo], cwd=_RAIZ, env=entorno,
                       capture_output=True, text=True)

    assert "IMPORTABLE" in r.stdout, (
        "beat_scheduler dejó de ser importable sin PYTHONPATH:\n" + r.stderr[-800:]
    )
    print("OK raíz en sys.path sin PYTHONPATH")


def test_el_service_no_es_la_unica_garantia():
    """El unit del repo sigue pasando el flag, pero ya no es imprescindible."""
    import os

    ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "systemd", "celery-beat.service")
    with open(ruta, encoding="utf-8") as f:
        unit = f.read()

    assert "beat_scheduler:SchedulerDB" in unit, "la plantilla perdió el --scheduler"
    print("OK plantilla systemd coherente")


if __name__ == "__main__":
    test_scheduler_declarado_en_la_config()
    test_raiz_sigue_en_sys_path_tras_cwd_in_path()
    test_el_service_no_es_la_unica_garantia()
