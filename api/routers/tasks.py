import json
from typing import Any

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_db, require_role

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    name: str
    function: str
    minute: str = "*"
    hour: str = "*"
    day_of_week: str = "*"
    day_of_month: str = "*"
    month_of_year: str = "*"
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    credentials_id: int | None = None
    activa: bool = True


class TaskPatch(BaseModel):
    function: str | None = None
    minute: str | None = None
    hour: str | None = None
    day_of_week: str | None = None
    day_of_month: str | None = None
    month_of_year: str | None = None
    args: list[Any] | None = None
    kwargs: dict[str, Any] | None = None
    credentials_id: int | None = None
    activa: bool | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_tasks(conn=Depends(get_db), _=Depends(require_role("viewer"))):
    """Lista todas las tareas con el nombre del set de credenciales asignado."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT t.id, t.name, t.function, t.minute, t.hour, t.day_of_week,
                   t.day_of_month, t.month_of_year, t.args, t.kwargs,
                   t.credentials_id, c.name AS credentials_name, t.activa
            FROM scheduler_tasks t
            LEFT JOIN scheduler_credentials c ON c.id = t.credentials_id
            ORDER BY t.id
        """)
        return cur.fetchall()


@router.post("", status_code=201)
def create_task(body: TaskCreate, conn=Depends(get_db), _=Depends(require_role("admin"))):
    """Crea una nueva tarea. Requiere reiniciar los servicios para que surta efecto."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        try:
            cur.execute("""
                INSERT INTO scheduler_tasks
                    (name, function, minute, hour, day_of_week, day_of_month,
                     month_of_year, args, kwargs, credentials_id, activa)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (
                body.name, body.function, body.minute, body.hour,
                body.day_of_week, body.day_of_month, body.month_of_year,
                json.dumps(body.args), json.dumps(body.kwargs),
                body.credentials_id, body.activa,
            ))
            conn.commit()
            return cur.fetchone()
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{name}")
def update_task(name: str, body: TaskPatch, conn=Depends(get_db), _=Depends(require_role("admin"))):
    """Actualiza parcialmente una tarea. Solo los campos enviados se modifican."""
    campos = body.model_dump(exclude_none=True)
    if not campos:
        raise HTTPException(status_code=400, detail="No se enviaron campos para actualizar.")

    # Serializar args/kwargs a JSON si vienen en el body
    if "args" in campos:
        campos["args"] = json.dumps(campos["args"])
    if "kwargs" in campos:
        campos["kwargs"] = json.dumps(campos["kwargs"])

    set_clause = ", ".join(f"{k} = %s" for k in campos)
    valores = list(campos.values()) + [name]

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"UPDATE scheduler_tasks SET {set_clause} WHERE name = %s RETURNING *",
            valores,
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"Tarea '{name}' no encontrada.")
        conn.commit()
        return row


@router.delete("/{name}", status_code=204)
def delete_task(name: str, conn=Depends(get_db), _=Depends(require_role("admin"))):
    """Elimina una tarea permanentemente."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM scheduler_tasks WHERE name = %s", (name,))
        if cur.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"Tarea '{name}' no encontrada.")
        conn.commit()


@router.post("/{name}/run")
def run_task(name: str, conn=Depends(get_db), _=Depends(require_role("operator"))):
    """Encola la tarea en Redis para ejecución inmediata (requiere worker activo)."""
    from celery import Celery
    import os

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT t.function, t.args, t.kwargs, c.env_vars
            FROM scheduler_tasks t
            LEFT JOIN scheduler_credentials c ON c.id = t.credentials_id
            WHERE t.name = %s
        """, (name,))
        tarea = cur.fetchone()

    if not tarea:
        raise HTTPException(status_code=404, detail=f"Tarea '{name}' no encontrada.")

    kwargs = dict(tarea["kwargs"] or {})
    if tarea["env_vars"]:
        kwargs["env_config"] = dict(tarea["env_vars"])

    sender = Celery(broker=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    resultado = sender.send_task(
        tarea["function"],
        args=list(tarea["args"] or []),
        kwargs=kwargs,
    )

    return {"task_id": resultado.id, "status": "enqueued", "function": tarea["function"]}
