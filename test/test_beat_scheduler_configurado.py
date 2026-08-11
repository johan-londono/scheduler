"""Beat tiene que quedar apuntado a SchedulerDB desde la config.

    python test/test_beat_scheduler_configurado.py

Regresión real: el .service instalado en un servidor era anterior al scheduler
custom, así que corría `celery -A app beat` sin --scheduler. Mientras app.py
llenaba beat_schedule al importarse eso funcionaba de casualidad; al sacar ese
I/O del import, Beat arrancó con el schedule vacío y dejó de encolar todo sin
un solo mensaje de error. Las tareas manuales seguían funcionando, así que
parecía un problema de otra cosa.
"""

from app import celery_app


def test_scheduler_declarado_en_la_config():
    # Sin esto, `celery -A app beat` usa PersistentScheduler y lee
    # app.conf.beat_schedule, que está vacío a propósito.
    assert celery_app.conf.beat_scheduler == "beat_scheduler:SchedulerDB", (
        f"beat_scheduler apunta a {celery_app.conf.beat_scheduler!r}; Beat no leería la DB"
    )
    print("OK beat_scheduler en la config")


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
    test_el_service_no_es_la_unica_garantia()
