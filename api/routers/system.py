import psycopg2.extras
from fastapi import APIRouter, Depends

from api.deps import get_db, require_role

router = APIRouter()


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
