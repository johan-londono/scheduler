"""
Manejo de tablas de errores de envío DIAN.

Las tablas viven en la **DB del scheduler**, no en la de cada cliente: los tres
flujos reciben `scheduler_pool` y escriben ahí, con la columna cliente_key para
distinguir de quién es cada error.

Las tablas se crean automáticamente con CREATE TABLE IF NOT EXISTS
la primera vez que se procesa un cliente, sin necesidad de
migraciones manuales.

Tablas:
  - dianenvio_errores            → facturas
  - dianenvio_errores_docsoporte → documentos soporte
  - dianenvio_errores_nc         → notas crédito
"""
import asyncpg


# ─────────────────────────────────────────────────────────────────────────────
# CONSULTA COMÚN: último error registrado por documento
# ─────────────────────────────────────────────────────────────────────────────

# tabla y columna de id para cada tipo de documento
TABLAS = {
    "facturas":   ("dianenvio_errores",            "factura_id"),
    "nc":         ("dianenvio_errores_nc",         "documento_id"),
    "docsoporte": ("dianenvio_errores_docsoporte", "documento_id"),
}


async def ultimos_errores(conn: asyncpg.Connection, tipo: str, ids: list) -> dict:
    """Último error de cada documento, indexado por id.

    Una sola consulta para todos los ids: es lo que convierte "hay 12 documentos
    atascados" en "estos 12, y por esto".
    """
    if not ids:
        return {}

    tabla, columna = TABLAS[tipo]
    rows = await conn.fetch(f"""
        SELECT DISTINCT ON ({columna})
               {columna} AS doc_id, error_codigo, error_razon, error_mensaje,
               intento_numero, created_at
        FROM   {tabla}
        WHERE  {columna} = ANY($1::int[])
        ORDER  BY {columna}, id DESC
    """, list(ids))
    return {r["doc_id"]: dict(r) for r in rows}


# ─────────────────────────────────────────────────────────────────────────────
# FACTURAS
# ─────────────────────────────────────────────────────────────────────────────

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
    """Crea la tabla de errores de facturas si no existe."""
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
    """Registra el error de un intento fallido de factura."""
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


# ─────────────────────────────────────────────────────────────────────────────
# DOCUMENTOS SOPORTE
# ─────────────────────────────────────────────────────────────────────────────

_CREATE_DOCSOPORTE_TABLE = """
    CREATE TABLE IF NOT EXISTS dianenvio_errores_docsoporte (
        id              SERIAL PRIMARY KEY,
        documento_id    INTEGER       NOT NULL,
        prefijo         VARCHAR(20),
        consecutivo     VARCHAR(50),
        intento_numero  INTEGER       NOT NULL,
        error_codigo    VARCHAR(100),
        error_razon     VARCHAR(500),
        error_mensaje   TEXT,
        cliente_key     VARCHAR(100),
        created_at      TIMESTAMPTZ   DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_dianenvio_err_ds_documento_id
        ON dianenvio_errores_docsoporte (documento_id);
    CREATE INDEX IF NOT EXISTS idx_dianenvio_err_ds_created_at
        ON dianenvio_errores_docsoporte (created_at DESC);
"""

_INSERT_DOCSOPORTE = """
    INSERT INTO dianenvio_errores_docsoporte
        (documento_id, prefijo, consecutivo, intento_numero,
         error_codigo, error_razon, error_mensaje, cliente_key)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""

_COUNT_INTENTOS_DOCSOPORTE = """
    SELECT COUNT(*) FROM dianenvio_errores_docsoporte WHERE documento_id = $1
"""


async def ensure_docsoporte_error_table(conn: asyncpg.Connection) -> None:
    """Crea la tabla de errores de documentos soporte si no existe."""
    await conn.execute(_CREATE_DOCSOPORTE_TABLE)


async def insert_docsoporte_error(
    conn: asyncpg.Connection,
    *,
    documento_id: int,
    prefijo: str,
    consecutivo: str,
    intento_numero: int,
    error_codigo: str,
    error_razon: str,
    error_mensaje: str,
    cliente_key: str,
) -> None:
    """Registra el error de un intento fallido de documento soporte."""
    codigo = str(error_codigo)[:100] if error_codigo is not None else None
    razon  = str(error_razon)[:500]  if error_razon  is not None else None

    await conn.execute(
        _INSERT_DOCSOPORTE,
        documento_id,
        prefijo,
        str(consecutivo),
        intento_numero,
        codigo,
        razon,
        error_mensaje,
        cliente_key,
    )


async def get_intentos_docsoporte(conn: asyncpg.Connection, documento_id: int) -> int:
    """Retorna el número de intentos fallidos previos para un documento soporte."""
    row = await conn.fetchrow(_COUNT_INTENTOS_DOCSOPORTE, documento_id)
    return int(row[0]) if row else 0


# ─────────────────────────────────────────────────────────────────────────────
# NOTAS CRÉDITO
# ─────────────────────────────────────────────────────────────────────────────

_CREATE_NC_TABLE = """
    CREATE TABLE IF NOT EXISTS dianenvio_errores_nc (
        id              SERIAL PRIMARY KEY,
        documento_id    INTEGER       NOT NULL,
        intento_numero  INTEGER       NOT NULL,
        error_codigo    VARCHAR(100),
        error_razon     VARCHAR(500),
        error_mensaje   TEXT,
        cliente_key     VARCHAR(100),
        created_at      TIMESTAMPTZ   DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_dianenvio_err_nc_documento_id
        ON dianenvio_errores_nc (documento_id);
    CREATE INDEX IF NOT EXISTS idx_dianenvio_err_nc_created_at
        ON dianenvio_errores_nc (created_at DESC);
"""

_INSERT_NC = """
    INSERT INTO dianenvio_errores_nc
        (documento_id, intento_numero,
         error_codigo, error_razon, error_mensaje, cliente_key)
    VALUES ($1, $2, $3, $4, $5, $6)
"""


async def ensure_nc_error_table(conn: asyncpg.Connection) -> None:
    """Crea la tabla de errores de notas crédito si no existe."""
    await conn.execute(_CREATE_NC_TABLE)


async def insert_nc_error(
    conn: asyncpg.Connection,
    *,
    documento_id: int,
    intento_numero: int,
    error_codigo: str,
    error_razon: str,
    error_mensaje: str,
    cliente_key: str,
) -> None:
    """Registra el error de un intento fallido de nota crédito."""
    codigo = str(error_codigo)[:100] if error_codigo is not None else None
    razon  = str(error_razon)[:500]  if error_razon  is not None else None

    await conn.execute(
        _INSERT_NC,
        documento_id,
        intento_numero,
        codigo,
        razon,
        error_mensaje,
        cliente_key,
    )
