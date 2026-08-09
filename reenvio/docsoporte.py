"""
Lógica de reenvío automático de documentos soporte por cliente.

Reglas:
- Solo se procesan documentos con diancufe IS NULL y dianenviado falso.
- Máximo MAX_INTENTOS intentos por documento (contados en dianenvio_errores_docsoporte).
- Si transmitir=False, los documentos se marcan como omitidos.
- Un lock por key_cli previene ejecuciones concurrentes del mismo cliente.
"""
import asyncio
import asyncpg
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
from reenvio.config import MAX_INTENTOS, filtro_fecha_sql
from reenvio.errores import (
    ensure_docsoporte_error_table,
    insert_docsoporte_error,
    get_intentos_docsoporte,
    ultimos_errores,
)

_client_locks: dict[str, asyncio.Lock] = {}

_QUERY_PENDIENTES = f"""
    SELECT cd.id,
           csf.prefijo,
           cd.consecutivoresolucion::text AS consecutivo
    FROM   contabledocsoportes cd
    JOIN   clienteserialfacturas csf ON cd.clienteserialfacturas_id = csf.id
    WHERE  cd.clienteserialfacturas_id > 0
      AND  (cd.dianenviado IS NULL OR cd.dianenviado = false)
      AND  cd.diancufe IS NULL
      {filtro_fecha_sql("cd.created_at")}
    ORDER  BY cd.created_at ASC
"""


async def llamar_getcuds(token: str, key_cli: str, consecutivo: str, prefijo: str) -> dict:
    """Llama a GET /api/v1/docsoporte/getcuds/{data_b64} y retorna el resultado."""
    return await llamar_api("docsoporte/getcuds", token, {
        "key_cli":                key_cli,
        "consecutivo_docsoporte": consecutivo,
        "prefijo_docsoporte":     str(prefijo),
    })


async def reenviar_cliente_docsoporte(
    cliente: dict,
    token: str,
    scheduler_pool: asyncpg.Pool,
) -> dict:
    """
    Punto de entrada para reenviar documentos soporte pendientes de un cliente.
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
            documentos = await conn.fetch(_QUERY_PENDIENTES)

        async with scheduler_pool.acquire() as conn:
            await ensure_docsoporte_error_table(conn)

        if not documentos:
            return resumen(key_cli, nombre, total=0)

        exitosas = fallidas = omitidas = ya_emitidas = agotadas = 0
        fallidas_detalle: list[dict] = []
        atascados: list[tuple] = []

        for doc in documentos:
            prefijo     = doc['prefijo']
            consecutivo = doc['consecutivo']
            doc_id      = doc['id']

            async with scheduler_pool.acquire() as conn:
                intentos_previos = await get_intentos_docsoporte(conn, doc_id)

            if intentos_previos >= MAX_INTENTOS:
                # Agotado, no omitido: nadie va a reintentarlo nunca más y el
                # correo tiene que decirlo en vez de contarlo como normal.
                agotadas += 1
                atascados.append((doc_id, f"DS {prefijo}-{consecutivo}", intentos_previos))
                print(f"    -> DS {prefijo}-{consecutivo} [AGOTADA] max intentos alcanzado ({intentos_previos})")
                continue

            intento = intentos_previos + 1
            print(f"    -> DS {prefijo}-{consecutivo} (intento {intento}/{MAX_INTENTOS})", end=' ', flush=True)

            try:
                result = await llamar_getcuds(token, key_cli, consecutivo, prefijo)
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
                procesados = exitosas + fallidas + omitidas + ya_emitidas + agotadas
                omitidas += len(documentos) - procesados
                print("[OMITIDA] transmitir=False")
                break

            else:
                fallidas += 1
                print(f"[FAIL] {reason_code}: {str(result.get('message', ''))[:60]}")

                fallidas_detalle.append(
                    detalle_fallo(key_cli, nombre, f"DS {prefijo}-{consecutivo}", result)
                )

                await registrar_error(
                    scheduler_pool, insert_docsoporte_error,
                    documento_id=doc_id,
                    prefijo=prefijo,
                    consecutivo=consecutivo,
                    intento_numero=intento,
                    error_codigo=str(reason_code) if reason_code is not None else None,
                    error_razon=str(result.get('reason', '')),
                    error_mensaje=error_mensaje(result),
                    cliente_key=key_cli,
                )

        async with scheduler_pool.acquire() as conn:
            errores = await ultimos_errores(conn, "docsoporte", [d[0] for d in atascados])

        return resumen(key_cli, nombre,
                       total=len(documentos),
                       exitosas=exitosas,
                       fallidas=fallidas,
                       omitidas=omitidas,
                       ya_emitidas=ya_emitidas,
                       agotadas=agotadas,
                       fallidas_detalle=fallidas_detalle,
                       agotadas_detalle=[
                           documento_atascado(key_cli, nombre, etiqueta, intentos, errores.get(doc_id))
                           for doc_id, etiqueta, intentos in atascados
                       ])

    except (asyncpg.PostgresError, OSError) as exc:
        print(f"[ERROR] Conexión DB — {nombre} ({key_cli}): {exc}", file=sys.stderr)
        return resumen(key_cli, nombre, total=0, connection_error=str(exc))

    except Exception as exc:
        print(f"[ERROR] Excepción inesperada en docsoporte — {nombre} ({key_cli}):", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return resumen(key_cli, nombre, total=0, connection_error=f"excepcion: {exc}")

    finally:
        if pool:
            await pool.close()
