"""
Lógica central de reenvío automático por cliente.

Reglas:
- Solo se procesan facturas con diancufe IS NULL (nunca aceptadas por DIAN).
- Máximo MAX_INTENTOS intentos por factura (diannumeroenvios se incrementa
  dentro del endpoint getCufe antes del envío, por lo que el filtro usa < MAX_INTENTOS).
- Si transmitir=False, las facturas se marcan como omitidas y no se loguean errores.
- Un lock por key_cli previene ejecuciones concurrentes del mismo cliente.

Las piezas compartidas con NC y documentos de soporte viven en comun.py.
"""
import asyncio
import asyncpg
import httpx
import sys
import traceback

from reenvio.comun import (
    clasificar,
    cufe,
    detalle_fallo,
    documento_atascado,
    error_mensaje,
    llamar_api,
    registrar_error,
    resumen,
)
from reenvio.config import MAX_INTENTOS, API_PYTHON_URL, filtro_fecha_sql
from reenvio.errores import ensure_error_table, insert_error, ultimos_errores

# Locks por cliente para evitar doble-incremento de diannumeroenvios
# si el servicio se invoca concurrentemente
_client_locks: dict[str, asyncio.Lock] = {}

# current_schema() evita elegir la variante equivocada de la consulta si el
# cliente tiene otra tabla 'facturas' en un esquema secundario.
_CHECK_PREFIJO_COL = """
    SELECT column_name FROM information_schema.columns
    WHERE  table_name = 'facturas'
      AND  column_name = 'prefijo'
      AND  table_schema = current_schema()
"""

_FILTRO_FECHA = filtro_fecha_sql("f.created_at")

_QUERY_PENDIENTES_DIRECTO = f"""
    SELECT f.id,
           f.prefijo,
           f.consecutivo,
           COALESCE(f.diannumeroenvios, 0) AS intentos_previos
    FROM   facturas f
    WHERE  f.modalidadpago_id = 2
      AND  f.diancufe IS NULL
      AND  COALESCE(f.diannumeroenvios, 0) < $1
      AND  f.estado_id = 1
      {_FILTRO_FECHA}
    ORDER  BY f.created_at ASC
"""

_QUERY_PENDIENTES_JOIN = f"""
    SELECT f.id,
           csf.prefijo,
           f.consecutivo,
           COALESCE(f.diannumeroenvios, 0) AS intentos_previos
    FROM   facturas f
    JOIN   clienteserialfacturas csf ON f.clienteserialfactura_id = csf.id
    WHERE  f.modalidadpago_id = 2
      AND  f.diancufe IS NULL
      AND  f.estado_id = 1
      AND  COALESCE(f.diannumeroenvios, 0) < $1
      {_FILTRO_FECHA}
    ORDER  BY f.created_at ASC
"""

# Facturas que ya gastaron todos los intentos: el filtro de pendientes las
# excluye, así que sin esta consulta desaparecen del reporte para siempre.
# Devuelve los documentos, no un conteo: "hay 12 atascadas" no sirve para
# arreglarlas, "estas 12 y por esto" sí.
_QUERY_AGOTADAS = f"""
    SELECT f.id,
           f.consecutivo,
           COALESCE(f.diannumeroenvios, 0) AS intentos
    FROM   facturas f
    WHERE  f.modalidadpago_id = 2
      AND  f.diancufe IS NULL
      AND  f.estado_id = 1
      AND  COALESCE(f.diannumeroenvios, 0) >= $1
      {_FILTRO_FECHA}
    ORDER  BY f.created_at ASC
"""


async def obtener_token(username: str, password: str) -> str:
    """Autentica contra la API y retorna el Bearer token JWT."""
    url = f"{API_PYTHON_URL}/api/v1/token"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.post(
            url,
            json={"email": username, "password": password},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Error al obtener token [{resp.status_code}]: {resp.text[:500]}"
            )
        return resp.json()["access_token"]


async def llamar_getcufe(token: str, key_cli: str, consecutivo: str, prefijo: str) -> dict:
    """Llama a GET /api/v1/invoice/getcufe/{data_b64} y retorna el resultado."""
    return await llamar_api("invoice/getcufe", token, {
        "key_cli": key_cli,
        "consecutivo_factura": int(consecutivo),
        "prefijo_factura": str(prefijo),
    })


async def _fichas_atascadas(conn, key_cli: str, nombre: str, filas) -> list:
    """Cruza las facturas sin intentos restantes con su último error registrado."""
    errores = await ultimos_errores(conn, "facturas", [f["id"] for f in filas])
    return [
        documento_atascado(
            key_cli, nombre,
            f"FE {f['consecutivo']}",
            f["intentos"],
            errores.get(f["id"]),
        )
        for f in filas
    ]


async def reenviar_cliente(cliente: dict, token: str, central_pool: asyncpg.Pool) -> dict:
    """
    Punto de entrada para reenviar facturas pendientes de un cliente.
    Adquiere un lock por key_cli antes de procesar.
    """
    key_cli = cliente['key_cli']

    if key_cli not in _client_locks:
        _client_locks[key_cli] = asyncio.Lock()

    async with _client_locks[key_cli]:
        return await _procesar_cliente(cliente, token, central_pool)


async def _procesar_cliente(cliente: dict, token: str, central_pool: asyncpg.Pool) -> dict:
    key_cli = cliente['key_cli']
    nombre  = cliente['nombre_cliente']

    pool = None
    try:
        pool = await asyncpg.create_pool(
            host=cliente['ip_db'],
            database=cliente['nombre_db'],
            user=cliente['user_db'],
            password=cliente['password_db'],
            port=int(cliente['puerto_db']),
            min_size=1,
            max_size=3,
        )

        async with pool.acquire() as conn:
            col = await conn.fetchrow(_CHECK_PREFIJO_COL)
            query = _QUERY_PENDIENTES_DIRECTO if col else _QUERY_PENDIENTES_JOIN
            facturas = await conn.fetch(query, MAX_INTENTOS)
            atascadas = await conn.fetch(_QUERY_AGOTADAS, MAX_INTENTOS)

        async with central_pool.acquire() as conn:
            await ensure_error_table(conn)
            agotadas_detalle = await _fichas_atascadas(conn, key_cli, nombre, atascadas)

        if not facturas:
            return resumen(key_cli, nombre, total=0, agotadas=len(atascadas),
                           agotadas_detalle=agotadas_detalle)

        exitosas = fallidas = omitidas = ya_emitidas = 0
        fallidas_detalle: list[dict] = []

        for factura in facturas:
            prefijo     = factura['prefijo']
            consecutivo = factura['consecutivo']
            factura_id  = factura['id']
            intento     = factura['intentos_previos'] + 1

            print(f"    -> {prefijo}-{consecutivo} (intento {intento}/{MAX_INTENTOS})", end=' ', flush=True)

            try:
                result = await llamar_getcufe(token, key_cli, consecutivo, prefijo)
            except Exception as exc:
                result = {
                    'succeeded':  False,
                    'reasonCode': 'UNEXPECTED_ERROR',
                    'reason':     'Excepción no controlada',
                    'message':    str(exc)[:500],
                    'detail':     '',
                }

            estado      = clasificar(result)
            reason_code = result.get('reasonCode', '')

            if estado == 'exito':
                exitosas += 1
                print(f"[OK] cufe={cufe(result)}...")

            elif estado == 'ya_emitido':
                ya_emitidas += 1
                print("[YA EMITIDO] la DIAN ya tenía el documento aceptado")

            elif estado == 'omitido':
                omitidas += len(facturas) - (exitosas + fallidas + omitidas + ya_emitidas)
                print("[OMITIDA] transmitir=False")
                break

            else:
                fallidas += 1
                razon   = str(result.get('reason',  '') or '')
                mensaje = str(result.get('message', '') or '')
                print(f"[FAIL] {reason_code}: {razon} — {mensaje}")

                fallidas_detalle.append(
                    detalle_fallo(key_cli, nombre, f"{prefijo}-{consecutivo}", result)
                )

                await registrar_error(
                    central_pool, insert_error,
                    factura_id=factura_id,
                    prefijo=prefijo,
                    consecutivo=consecutivo,
                    intento_numero=intento,
                    error_codigo=str(reason_code) if reason_code is not None else None,
                    error_razon=razon,
                    error_mensaje=error_mensaje(result),
                    cliente_key=key_cli,
                )

        return resumen(key_cli, nombre,
                       total=len(facturas),
                       exitosas=exitosas,
                       fallidas=fallidas,
                       omitidas=omitidas,
                       ya_emitidas=ya_emitidas,
                       agotadas=len(atascadas),
                       fallidas_detalle=fallidas_detalle,
                       agotadas_detalle=agotadas_detalle)

    except (asyncpg.PostgresError, OSError) as exc:
        print(f"[ERROR] Conexión DB — {nombre} ({key_cli}): {exc}", file=sys.stderr)
        return resumen(key_cli, nombre, total=0, connection_error=str(exc))

    except Exception as exc:
        print(f"[ERROR] Excepción inesperada en facturas — {nombre} ({key_cli}):", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return resumen(key_cli, nombre, total=0, connection_error=f"excepcion: {exc}")

    finally:
        if pool:
            await pool.close()
