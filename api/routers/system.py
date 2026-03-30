import os
import subprocess

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_db, require_role

router = APIRouter()

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@router.get("/status")
def status(conn=Depends(get_db), _=Depends(require_role("viewer"))):
    """Muestra las tareas activas actualmente registradas en la DB."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT t.name, t.function,
                   t.minute, t.hour, t.day_of_week, t.day_of_month, t.month_of_year,
                   t.activa, c.name AS credentials
            FROM scheduler_tasks t
            LEFT JOIN scheduler_credentials c ON c.id = t.credentials_id
            ORDER BY t.id
        """)
        return {"tasks": cur.fetchall()}


@router.post("/restart")
def restart_services(_=Depends(require_role("admin"))):
    """Reinicia celery-worker y celery-beat para aplicar cambios en la tabla.

    Requiere que el proceso uvicorn tenga permiso de ejecutar systemctl sin
    contraseña. Agregar a /etc/sudoers:
        www-data ALL=(ALL) NOPASSWD: /bin/systemctl restart celery-worker celery-beat
        www-data ALL=(ALL) NOPASSWD: /bin/systemctl daemon-reload
    """
    try:
        subprocess.run(
            ["sudo", "systemctl", "daemon-reload"],
            check=True, capture_output=True, timeout=15,
        )
        resultado = subprocess.run(
            ["sudo", "systemctl", "restart", "celery-worker", "celery-beat"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return {"status": "restarted", "detail": resultado.stdout or "OK"}
    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al reiniciar: {e.stderr or str(e)}",
        )
