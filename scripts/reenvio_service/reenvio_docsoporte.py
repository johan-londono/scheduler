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
import base64
import httpx
import json
import sys
import traceback

from reenvio_service.config import MAX_INTENTOS, API_PYTHON_URL
from reenvio_service.error_log import (
    ensure_docsoporte_error_table,
    insert_docsoporte_error,
    get_intentos_docsoporte,
)

_client_locks: dict[str, asyncio.Lock] = {}

_QUERY_PENDIENTES = """
    SELECT cd.id,
           csf.prefijo,
           cd.consecutivoresolucion::text AS consecutivo
    FROM   contabledocsoportes cd
    JOIN   clienteserialfacturas csf ON cd.clienteserialfacturas_id = csf.id
    WHERE  cd.clienteserialfacturas_id > 0
      AND  (cd.dianenviado IS NULL OR cd.dianenviado = false)
      AND  cd.diancufe IS NULL
    ORDER  BY cd.created_at ASC
"""


async def llamar_getcuds(token: str, key_cli: str, consecutivo: str, prefijo: str) -> dict:
    """Llama a GET /api/v1/docsoporte/getcuds/{data_b64} y retorna el resultado."""
    payload = json.dumps({
        "key_cli":    key_cli,
        "consecutivo": consecutivo,
        "prefijo":    str(prefijo),
    })
    data_b64 = base64.urlsafe_b64encode(payload.encode()).decode()

    url = f"{API_PYTHON_URL}/api/v1/docsoporte/getcuds/{data_b64}"
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
            return _summary(key_cli, nombre, total=0)

        exitosas = fallidas = omitidas = 0

        for doc in documentos:
            prefijo     = doc['prefijo']
            consecutivo = doc['consecutivo']
            doc_id      = doc['id']

            async with scheduler_pool.acquire() as conn:
                intentos_previos = await get_intentos_docsoporte(conn, doc_id)

            if intentos_previos >= MAX_INTENTOS:
                omitidas += 1
                print(f"    -> DS {prefijo}-{consecutivo} [OMITIDA] max intentos alcanzado ({intentos_previos})")
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

            succeeded   = result.get('succeeded', False)
            reason_code = result.get('reasonCode', '')

            if succeeded or reason_code == 'ALREADY_EMITTED':
                exitosas += 1
                cufe_short = result.get('cufe', '')[:20]
                print(f"[OK] cufe={cufe_short}...")

            elif reason_code == 'TRANSMISSION_DISABLED':
                remaining  = len(documentos) - (exitosas + fallidas + omitidas)
                omitidas  += remaining
                print(f"[OMITIDA] transmitir=False")
                break

            else:
                fallidas += 1
                print(f"[FAIL] {reason_code}: {result.get('message', '')[:60]}")

                async with scheduler_pool.acquire() as conn:
                    await insert_docsoporte_error(
                        conn,
                        documento_id=doc_id,
                        prefijo=prefijo,
                        consecutivo=consecutivo,
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
                        total=len(documentos),
                        exitosas=exitosas,
                        fallidas=fallidas,
                        omitidas=omitidas)

    except (asyncpg.PostgresError, OSError) as exc:
        print(f"[ERROR] Conexión DB — {nombre} ({key_cli}): {exc}", file=sys.stderr)
        return _summary(key_cli, nombre, total=0, connection_error=str(exc))

    except Exception as exc:
        print(f"[ERROR] Excepción inesperada en docsoporte — {nombre} ({key_cli}):", file=sys.stderr)
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
