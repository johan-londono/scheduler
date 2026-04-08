"""
Lógica de reenvío automático de notas crédito por cliente.

Reglas:
- Solo se procesan NC con diancufe IS NULL (nunca aceptadas por DIAN).
- Máximo MAX_INTENTOS intentos por NC (diannumeroenvios se incrementa
  dentro del endpoint getnotacredito antes del envío, por lo que el filtro usa < MAX_INTENTOS).
- La búsqueda en getnotacredito se hace SIEMPRE por nc_id (nunca por prefijo+consecutivo).
- El parámetro `referenciada` se infiere del registro: si la NC tiene un
  numerodocumento > 0, se asume que referencia una factura (Operación 20);
  en caso contrario se usa Operación 22 (sin referencia).
- El display usa nc.consecutivo y clienteserialfactura_id → clienteserialfacturas.prefijo.
- Si transmitir=False, las NC se marcan como omitidas y no se loguean errores.
- Un lock por key_cli previene ejecuciones concurrentes del mismo cliente.
"""
import asyncio
import asyncpg
import base64
import httpx
import json
import sys
import traceback

from reenvio_service.config import MAX_INTENTOS, API_PYTHON_URL, filtro_fecha_sql
from reenvio_service.error_log import ensure_nc_error_table, insert_nc_error

_client_locks: dict[str, asyncio.Lock] = {}

# tipodocumentointerno_id = 3 → Nota Crédito electrónica
# Prefijo obtenido via clienteserialfactura_id → clienteserialfacturas.prefijo
# Búsqueda en getnotacredito siempre por nc.id
_QUERY_PENDIENTES = f"""
    SELECT nc.id,
           COALESCE(nc.diannumeroenvios, 0) AS intentos_previos,
           COALESCE(nc.consecutivo, nc.id) AS consecutivo,
           csf.prefijo,
           nc.numerodocumento
    FROM   contablenotascreditos nc
    LEFT   JOIN clienteserialfacturas csf ON nc.clienteserialfactura_id = csf.id
    WHERE  nc.tipodocumentointerno_id = 3
      AND  nc.diancufe IS NULL
      AND  COALESCE(nc.diannumeroenvios, 0) < $1
      {filtro_fecha_sql("nc.created_at")}
    ORDER  BY nc.created_at ASC
"""

# Fallback para clientes cuya tabla no tiene la columna consecutivo
_QUERY_PENDIENTES_SIN_CONSECUTIVO = f"""
    SELECT nc.id,
           COALESCE(nc.diannumeroenvios, 0) AS intentos_previos,
           nc.id                            AS consecutivo,
           csf.prefijo,
           nc.numerodocumento
    FROM   contablenotascreditos nc
    LEFT   JOIN clienteserialfacturas csf ON nc.clienteserialfactura_id = csf.id
    WHERE  nc.tipodocumentointerno_id = 3
      AND  nc.diancufe IS NULL
      AND  COALESCE(nc.diannumeroenvios, 0) < $1
      {filtro_fecha_sql("nc.created_at")}
    ORDER  BY nc.created_at ASC
"""


async def llamar_getnotacredito(
    token: str,
    key_cli: str,
    nc_id: int,
    referenciada: bool,
) -> dict:
    """Llama a GET /api/v1/notacredito/getnotacredito/{data_b64} y retorna el resultado."""
    payload = json.dumps({
        "key_cli":     key_cli,
        "nc_id":       nc_id,
        "referenciada": referenciada,
    })
    data_b64 = base64.urlsafe_b64encode(payload.encode()).decode()

    url = f"{API_PYTHON_URL}/api/v1/notacredito/getnotacredito/{data_b64}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if not resp.is_success:
            try:
                body  = resp.json()
                inner = body.get("detail", {})
                if isinstance(inner, dict):
                    return {
                        "succeeded":  False,
                        "reasonCode": inner.get("reasonCode", resp.status_code),
                        "reason":     inner.get("reason", ""),
                        "message":    inner.get("message", ""),
                        "detail":     inner.get("detail", []),
                    }
            except Exception:
                pass
            return {
                "succeeded":  False,
                "reasonCode": f"HTTP_{resp.status_code}",
                "reason":     f"Error HTTP {resp.status_code}",
                "message":    resp.text[:500],
                "detail":     [],
            }
        return resp.json()


async def reenviar_cliente_nc(
    cliente: dict,
    token: str,
    scheduler_pool: asyncpg.Pool,
) -> dict:
    """
    Punto de entrada para reenviar notas crédito pendientes de un cliente.
    """
    key_cli = cliente['key_cli']

    if key_cli not in _client_locks:
        _client_locks[key_cli] = asyncio.Lock()

    async with _client_locks[key_cli]:
        return await _procesar_cliente(cliente, token, scheduler_pool)


async def _procesar_cliente(
    cliente: dict,
    token: str,
    scheduler_pool: asyncpg.Pool,
) -> dict:
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
            try:
                ncs = await conn.fetch(_QUERY_PENDIENTES, MAX_INTENTOS)
            except asyncpg.exceptions.UndefinedColumnError:
                ncs = await conn.fetch(_QUERY_PENDIENTES_SIN_CONSECUTIVO, MAX_INTENTOS)

        async with scheduler_pool.acquire() as conn:
            await ensure_nc_error_table(conn)

        if not ncs:
            return _summary(key_cli, nombre, total=0)

        exitosas = fallidas = omitidas = 0

        for nc in ncs:
            nc_id            = nc['id']
            intentos_previos = nc['intentos_previos']
            consecutivo      = nc['consecutivo']
            prefijo          = nc['prefijo'] or 'NC'
            numerodocumento  = nc['numerodocumento']
            intento          = intentos_previos + 1

            # Inferir si la NC referencia una factura
            referenciada = bool(numerodocumento and int(numerodocumento) > 0)

            tipo_ref = "ref" if referenciada else "sin-ref"
            print(f"    -> NC {prefijo}-{consecutivo} ({tipo_ref}, intento {intento}/{MAX_INTENTOS})", end=' ', flush=True)

            try:
                result = await llamar_getnotacredito(token, key_cli, nc_id, referenciada)
            except Exception as exc:
                result = {
                    'succeeded':  False,
                    'reasonCode': 'UNEXPECTED_ERROR',
                    'reason':     'Excepción no controlada',
                    'message':    str(exc)[:500],
                    'detail':     '',
                }

            succeeded   = result.get('succeeded', False)
            reason_code = result.get('reasonCode', '')

            if succeeded or reason_code == 'ALREADY_EMITTED':
                exitosas += 1
                cufe_short = result.get('cufe', '')[:20]
                print(f"[OK] cufe={cufe_short}...")

            elif reason_code == 'TRANSMISSION_DISABLED':
                remaining  = len(ncs) - (exitosas + fallidas + omitidas)
                omitidas  += remaining
                print(f"[OMITIDA] transmitir=False")
                break

            else:
                fallidas += 1
                print(f"[FAIL] {reason_code}: {result.get('message', '')[:60]}")

                async with scheduler_pool.acquire() as conn:
                    await insert_nc_error(
                        conn,
                        documento_id=nc_id,
                        intento_numero=intento,
                        error_codigo=str(reason_code) if reason_code is not None else None,
                        error_razon=str(result.get('reason', '')),
                        error_mensaje=json.dumps(
                            {
                                'message': str(result.get('message', '')),
                                'detail':  str(result.get('detail', '')),
                            },
                            ensure_ascii=False,
                        ),
                        cliente_key=key_cli,
                    )

        return _summary(key_cli, nombre,
                        total=len(ncs),
                        exitosas=exitosas,
                        fallidas=fallidas,
                        omitidas=omitidas)

    except (asyncpg.PostgresError, OSError) as exc:
        print(f"[ERROR] Conexión DB — {nombre} ({key_cli}): {exc}", file=sys.stderr)
        return _summary(key_cli, nombre, total=0, connection_error=str(exc))

    except Exception as exc:
        print(f"[ERROR] Excepción inesperada en NC — {nombre} ({key_cli}):", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return _summary(key_cli, nombre, total=0, connection_error=f"excepcion: {exc}")

    finally:
        if pool:
            await pool.close()


def _summary(
    key_cli: str,
    nombre: str,
    *,
    total: int = 0,
    exitosas: int = 0,
    fallidas: int = 0,
    omitidas: int = 0,
    connection_error: str = None,
) -> dict:
    s = {
        'key_cli':  key_cli,
        'nombre':   nombre,
        'total':    total,
        'exitosas': exitosas,
        'fallidas': fallidas,
        'omitidas': omitidas,
    }
    if connection_error:
        s['connection_error'] = connection_error
    return s
