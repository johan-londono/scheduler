from db import obtener_conexion


def get_db():
    """Dependency de FastAPI: abre una conexión por request y la cierra al terminar."""
    conn = obtener_conexion()
    try:
        yield conn
    finally:
        conn.close()
