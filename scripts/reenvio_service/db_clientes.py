"""
Conexión a la DB principal de esuite para obtener los clientes registrados.
"""
import asyncpg
from reenvio_service.config import (
    MAIN_DB_HOST, MAIN_DB_NAME, MAIN_DB_USER,
    MAIN_DB_PASSWORD, MAIN_DB_PORT,
    SCHEDULER_DB_HOST, SCHEDULER_DB_NAME, SCHEDULER_DB_USER,
    SCHEDULER_DB_PASSWORD, SCHEDULER_DB_PORT,
)


async def create_scheduler_pool() -> asyncpg.Pool:
    """Pool hacia la DB del scheduler (tablas de errores de reenvío DIAN)."""
    return await asyncpg.create_pool(
        host=SCHEDULER_DB_HOST,
        database=SCHEDULER_DB_NAME,
        user=SCHEDULER_DB_USER,
        password=SCHEDULER_DB_PASSWORD,
        port=SCHEDULER_DB_PORT,
        min_size=1,
        max_size=3,
    )


async def create_main_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=MAIN_DB_HOST,
        database=MAIN_DB_NAME,
        user=MAIN_DB_USER,
        password=MAIN_DB_PASSWORD,
        port=MAIN_DB_PORT,
        min_size=1,
        max_size=3,
    )


async def get_all_clientes(pool: asyncpg.Pool) -> list[dict]:
    """
    Retorna todos los clientes con transmitir=true.
    Estos son los únicos para los que tiene sentido intentar envíos.
    """
    rows = await pool.fetch("""
        SELECT id, key_cli, nombre_cliente,
               nombre_db, ip_db, puerto_db,
               user_db, password_db,
               produccion, transmitir
        FROM   clientes_conexiones_db
        WHERE  transmitir = true
        ORDER  BY id ASC
    """)
    return [dict(row) for row in rows]


async def get_cliente_by_key(pool: asyncpg.Pool, key_cli: str) -> dict | None:
    """Retorna un cliente específico por key_cli."""
    row = await pool.fetchrow("""
        SELECT id, key_cli, nombre_cliente,
               nombre_db, ip_db, puerto_db,
               user_db, password_db,
               produccion, transmitir
        FROM   clientes_conexiones_db
        WHERE  key_cli = $1
    """, key_cli)
    return dict(row) if row else None
