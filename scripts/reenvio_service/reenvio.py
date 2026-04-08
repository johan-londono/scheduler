"""
Lógica central de reenvío automático por cliente.

Reglas:
- Solo se procesan facturas con diancufe IS NULL (nunca aceptadas por DIAN).
- Máximo MAX_INTENTOS intentos por factura (diannumeroenvios se incrementa
  dentro del endpoint getCufe antes del envío, por lo que el filtro usa < MAX_INTENTOS).
- Si transmitir=False, las facturas se marcan como omitidas y no se loguean errores.
- Un lock por key_cli previene ejecuciones concurrentes del mismo cliente.
"""
import asyncio
import asyncpg
import httpx
import json
import base64
import sys
import traceback
from datetime import datetime

from reenvio_service.config import MAX_INTENTOS, API_PYTHON_URL, filtro_fecha_sql
from reenvio_service.error_log import insert_error

# Locks por cliente para evitar doble-incremento de diannumeroenvios
# si el servicio se invoca concurrentemente
_client_locks: dict[str, asyncio.Lock] = {}

_CHECK_PREFIJO_COL = """
    SELECT column_name FROM information_schema.columns
    WHERE  table_name = 'facturas' AND column_name = 'prefijo'
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
    payload = json.dumps({
        "key_cli": key_cli,
        "consecutivo_factura": int(consecutivo),
        "prefijo_factura": str(prefijo),
    })
    data_b64 = base64.urlsafe_b64encode(payload.encode()).decode()

    url = f"{API_PYTHON_URL}/api/v1/invoice/getcufe/{data_b64}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if not resp.is_success:
            try:
                body = resp.json()
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

        if not facturas:
            return _summary(key_cli, nombre, total=0)

        exitosas = fallidas = omitidas = 0
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

            succeeded   = result.get('succeeded', False)
            reason_code = result.get('reasonCode', '')

            if succeeded:
                exitosas += 1
                print(f"[OK] cufe={result.get('cufe', '')[:20]}...")

            elif reason_code == 'TRANSMISSION_DISABLED':
                remaining  = len(facturas) - (exitosas + fallidas + omitidas)
                omitidas  += remaining
                print(f"[OMITIDA] transmitir=False")
                break

            else:
                fallidas += 1
                razon   = str(result.get('reason',   '') or '')
                mensaje = str(result.get('message',  '') or '')
                raw_det = result.get('detail', '')
                # detail puede ser lista, dict o string según la API
                if isinstance(raw_det, list):
                    detalle_items = [str(d) for d in raw_det]
                elif isinstance(raw_det, dict):
                    detalle_items = [f"{k}: {v}" for k, v in raw_det.items()]
                elif raw_det:
                    detalle_items = [str(raw_det)]
                else:
                    detalle_items = []

                print(f"[FAIL] {reason_code}: {razon} — {mensaje}")

                fallidas_detalle.append({
                    'key_cli':  key_cli,
                    'cliente':  nombre,
                    'factura':  f"{prefijo}-{consecutivo}",
                    'codigo':   str(reason_code) if reason_code else '',
                    'razon':    razon,
                    'mensaje':  mensaje,
                    'detalle':  detalle_items,
                })

                async with central_pool.acquire() as conn:
                    await insert_error(
                        conn,
                        factura_id=factura_id,
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
                        total=len(facturas),
                        exitosas=exitosas,
                        fallidas=fallidas,
                        omitidas=omitidas,
                        fallidas_detalle=fallidas_detalle)

    except (asyncpg.PostgresError, OSError) as exc:
        print(f"[ERROR] Conexión DB — {nombre} ({key_cli}): {exc}", file=sys.stderr)
        return _summary(key_cli, nombre, total=0, connection_error=str(exc))

    except Exception as exc:
        print(f"[ERROR] Excepción inesperada en facturas — {nombre} ({key_cli}):", file=sys.stderr)
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
    fallidas_detalle: list = None,
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
    if fallidas_detalle:
        s['fallidas_detalle'] = fallidas_detalle
    if connection_error:
        s['connection_error'] = connection_error
    return s
