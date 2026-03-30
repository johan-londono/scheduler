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
    """Lee las tareas activas de la tabla scheduler_tasks y las retorna como lista de dicts."""
    conn = obtener_conexion()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT name, function, minute, hour, day_of_week, day_of_month,
                       month_of_year, args, kwargs, db_config
                FROM scheduler_tasks
                WHERE activa = TRUE
                ORDER BY id
            """)
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
