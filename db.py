import os
import psycopg2
import psycopg2.extras


def obtener_conexion():
    """Retorna una conexión a la base de datos del scheduler (SCHEDULER_DB_*)."""
    return psycopg2.connect(
        host=os.environ["SCHEDULER_DB_HOST"],
        port=int(os.environ.get("SCHEDULER_DB_PORT", 5432)),
        user=os.environ["SCHEDULER_DB_USER"],
        password=os.environ["SCHEDULER_DB_PASSWORD"],
        dbname=os.environ["SCHEDULER_DB_DATABASE"],
    )


def obtener_tareas_activas():
    """Lee las tareas activas con sus credenciales y las retorna como lista de dicts.

    El campo env_config en cada tarea contiene env_vars del set de credenciales
    referenciado (o None si la tarea no tiene credenciales asignadas).
    """
    conn = obtener_conexion()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT t.name, t.function, t.minute, t.hour, t.day_of_week,
                       t.day_of_month, t.month_of_year, t.args, t.kwargs,
                       c.env_vars AS env_config
                FROM scheduler_tasks t
                LEFT JOIN scheduler_credentials c ON c.id = t.credentials_id
                WHERE t.activa = TRUE
                ORDER BY t.id
            """)
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
