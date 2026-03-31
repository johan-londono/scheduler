"""
Manejo de la tabla dianenvio_errores en la DB de cada cliente.

La tabla se crea automáticamente con CREATE TABLE IF NOT EXISTS
la primera vez que se procesa el cliente, sin necesidad de
migraciones manuales.
"""
import asyncpg

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS dianenvio_errores (
        id              SERIAL PRIMARY KEY,
        factura_id      INTEGER       NOT NULL,
        prefijo         VARCHAR(20),
        consecutivo     INTEGER,
        intento_numero  INTEGER       NOT NULL,
        error_codigo    VARCHAR(100),
        error_razon     VARCHAR(500),
        error_mensaje   TEXT,
        cliente_key     VARCHAR(100),
        created_at      TIMESTAMPTZ   DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_dianenvio_errores_factura_id
        ON dianenvio_errores (factura_id);
    CREATE INDEX IF NOT EXISTS idx_dianenvio_errores_created_at
        ON dianenvio_errores (created_at DESC);
"""

_INSERT = """
    INSERT INTO dianenvio_errores
        (factura_id, prefijo, consecutivo, intento_numero,
         error_codigo, error_razon, error_mensaje, cliente_key)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""


async def ensure_error_table(conn: asyncpg.Connection) -> None:
    """Crea la tabla si no existe. Operación idempotente."""
    await conn.execute(_CREATE_TABLE)


async def insert_error(
    conn: asyncpg.Connection,
    *,
    factura_id: int,
    prefijo: str,
    consecutivo: int,
    intento_numero: int,
    error_codigo: str,
    error_razon: str,
    error_mensaje: str,
    cliente_key: str,
) -> None:
    """Registra el error de un intento fallido."""
    codigo = str(error_codigo)[:100] if error_codigo is not None else None
    razon  = str(error_razon)[:500]  if error_razon  is not None else None

    await conn.execute(
        _INSERT,
        factura_id,
        prefijo,
        consecutivo,
        intento_numero,
        codigo,
        razon,
        error_mensaje,
        cliente_key,
    )
