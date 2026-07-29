"""Scheduler de Celery Beat que relee scheduler_tasks en caliente.

Evita reiniciar celery-beat cuando se crea, edita, activa o desactiva una
tarea cuya función Python ya existe en el worker.

Uso:
    celery -A app beat --scheduler=beat_scheduler:SchedulerDB
"""

import logging
import time

from celery.beat import PersistentScheduler

from app import construir_schedule

logger = logging.getLogger(__name__)


class SchedulerDB(PersistentScheduler):
    """Sincroniza el beat_schedule contra la DB cada INTERVALO_RECARGA segundos."""

    INTERVALO_RECARGA = 60
    # max_interval fuerza a beat a despertar al menos cada minuto, si no
    # dormiría hasta la próxima tarea y la recarga nunca correría a tiempo.
    max_interval = INTERVALO_RECARGA

    def setup_schedule(self):
        super().setup_schedule()
        self._ultima_recarga = time.monotonic()

    def tick(self, *args, **kwargs):
        if time.monotonic() - self._ultima_recarga >= self.INTERVALO_RECARGA:
            self._ultima_recarga = time.monotonic()
            self._recargar()
        return super().tick(*args, **kwargs)

    def _recargar(self):
        # ponytail: poll cada 60s. Si hiciera falta latencia menor, LISTEN/NOTIFY
        # de Postgres sobre scheduler_tasks reemplaza el timer sin tocar el resto.
        try:
            schedule = construir_schedule()
        except Exception as exc:
            logger.error(f"No se pudo releer scheduler_tasks, se mantiene el schedule vigente: {exc}")
            return

        antes = set(self.schedule)
        # merge_inplace elimina las que ya no están y preserva last_run_at
        # de las que siguen, actualizando solo horario/args/kwargs.
        self.merge_inplace(schedule)
        despues = set(self.schedule)
        if antes != despues:
            logger.info(f"Schedule recargado: nuevas={sorted(despues - antes)} eliminadas={sorted(antes - despues)}")


def _autocheck():
    """Verifica que una recarga agregue, elimine y actualice entries correctamente."""
    from celery.schedules import crontab

    scheduler = SchedulerDB.__new__(SchedulerDB)
    scheduler.app = None
    scheduler._store = {"entries": {}}
    scheduler.Entry = _EntryFake

    scheduler.merge_inplace({
        "vieja": {"task": "t.a", "schedule": crontab(minute="0"), "args": [], "kwargs": {}},
        "estable": {"task": "t.b", "schedule": crontab(minute="0"), "args": [], "kwargs": {}},
    })
    scheduler.schedule["estable"].last_run_at = "MARCA"

    scheduler.merge_inplace({
        "estable": {"task": "t.b", "schedule": crontab(minute="30"), "args": [], "kwargs": {"x": 1}},
        "nueva": {"task": "t.c", "schedule": crontab(minute="0"), "args": [], "kwargs": {}},
    })

    assert set(scheduler.schedule) == {"estable", "nueva"}, scheduler.schedule
    assert scheduler.schedule["estable"].kwargs == {"x": 1}, "no aplicó los kwargs nuevos"
    assert scheduler.schedule["estable"].last_run_at == "MARCA", "perdió el last_run_at"
    print("OK")


class _EntryFake:
    """ScheduleEntry mínimo: solo lo que merge_inplace toca."""

    def __init__(self, name=None, task=None, schedule=None, args=(), kwargs=None, app=None, **_):
        self.name, self.task, self.schedule = name, task, schedule
        self.args, self.kwargs, self.options = args, kwargs or {}, {}
        self.last_run_at = None

    def update(self, other):
        self.__dict__.update({
            "task": other.task, "schedule": other.schedule,
            "args": other.args, "kwargs": other.kwargs, "options": other.options,
        })


if __name__ == "__main__":
    _autocheck()
