"""Piezas compartidas por los tres flujos de reenvío DIAN.

Estaban triplicadas en reenvio.py / reenvio_nc.py / reenvio_docsoporte.py y
divergieron: ALREADY_EMITTED contaba como éxito en dos de las tres copias, solo
facturas construía el detalle de fallos y solo dos creaban su tabla de errores.
Cada arreglo había que hacerlo tres veces y en la práctica no pasaba.
"""
import base64
import json
import sys

import httpx

from reenvio.config import API_PYTHON_URL

# La DIAN ya tenía el documento aceptado: no hay nada que reenviar, pero tampoco
# es un envío nuevo. Se cuenta aparte para no inflar "exitosas".
YA_EMITIDO = "ALREADY_EMITTED"

# El cliente tiene transmitir=False: se omite el resto de sus documentos.
TRANSMISION_DESACTIVADA = "TRANSMISSION_DISABLED"


async def llamar_api(recurso: str, token: str, payload: dict) -> dict:
    """GET /api/v1/<recurso>/<payload en base64url>.

    Devuelve siempre un dict con la forma de la API: los errores HTTP se
    traducen a {'succeeded': False, 'reasonCode': ...} en lugar de lanzar.
    """
    data_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    url = f"{API_PYTHON_URL}/api/v1/{recurso}/{data_b64}"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})

    if resp.is_success:
        return resp.json()

    try:
        inner = resp.json().get("detail", {})
    except Exception:
        inner = {}

    if isinstance(inner, dict) and inner:
        return {
            "succeeded":  False,
            "reasonCode": inner.get("reasonCode", resp.status_code),
            "reason":     inner.get("reason", ""),
            "message":    inner.get("message", ""),
            "detail":     inner.get("detail", []),
        }

    return {
        "succeeded":  False,
        "reasonCode": f"HTTP_{resp.status_code}",
        "reason":     f"Error HTTP {resp.status_code}",
        "message":    resp.text[:500],
        "detail":     [],
    }


def clasificar(result: dict) -> str:
    """'exito' | 'ya_emitido' | 'omitido' | 'fallo'.

    Un solo criterio para los tres tipos de documento: antes facturas contaba
    ALREADY_EMITTED como fallo y NC/DS como éxito, así que las cifras del correo
    no eran comparables entre sí.
    """
    if result.get("succeeded", False):
        return "exito"

    codigo = result.get("reasonCode", "")
    if codigo == YA_EMITIDO:
        return "ya_emitido"
    if codigo == TRANSMISION_DESACTIVADA:
        return "omitido"
    return "fallo"


# ── Causa de un documento atascado ────────────────────────────────────────────
#
# Un documento que agotó los intentos no se reintenta solo nunca más. Para poder
# hacer algo con él hay que saber de quién es el problema. Las reglas salen de
# los códigos que aparecen de verdad en dianenvio_errores*; ampliar aquí cuando
# surja uno nuevo.

CAUSA_DATOS   = "datos"     # falta información en la DB del cliente
CAUSA_DIAN    = "dian"      # la DIAN rechazó el documento
CAUSA_TECNICA = "tecnica"   # error de infraestructura o transitorio

ACCION_POR_CAUSA = {
    CAUSA_DATOS:   "Completar el dato faltante en la base de datos del cliente",
    CAUSA_DIAN:    "Corregir el documento según la validación de la DIAN",
    CAUSA_TECNICA: "Reintentable: reiniciar el contador de intentos del documento",
}

# El orden importa: un código 137 ("Error en validaciones Dian") con mensaje
# "Intente más tarde, Documento en proceso" es transitorio, no un rechazo.
_PATRONES_TECNICA = ("intente más tarde", "intente mas tarde", "en proceso",
                     "timeout", "connection", "temporarily", "503", "502")
_CODIGOS_TECNICA = {"UNEXPECTED_ERROR"}

_PATRONES_DATOS = ("missing", "no tiene", "requiere", "falta", "not found",
                   "no encontrad", "sin serial", "no existe", "null")
_CODIGOS_DATOS = {"NC_NO_SERIAL", "PROVIDER_NOT_FOUND", "404"}


def mensaje_legible(error_mensaje) -> str:
    """Saca el texto del JSON que se guarda en la columna error_mensaje."""
    if not error_mensaje:
        return ""
    try:
        datos = json.loads(error_mensaje)
    except (ValueError, TypeError):
        return str(error_mensaje)

    partes = [str(datos.get("message") or "")]
    detalle = datos.get("detail")
    if detalle and detalle not in ("[]", "{}", "None"):
        partes.append(str(detalle))
    return " — ".join(p for p in partes if p)


def clasificar_causa(codigo, razon, mensaje) -> str:
    """Devuelve CAUSA_DATOS | CAUSA_DIAN | CAUSA_TECNICA."""
    codigo = str(codigo or "")
    texto = f"{razon or ''} {mensaje or ''}".lower()

    if codigo.startswith("HTTP_") or codigo in _CODIGOS_TECNICA:
        return CAUSA_TECNICA
    if any(p in texto for p in _PATRONES_TECNICA):
        return CAUSA_TECNICA
    if codigo in _CODIGOS_DATOS or any(p in texto for p in _PATRONES_DATOS):
        return CAUSA_DATOS
    return CAUSA_DIAN


def documento_atascado(key_cli: str, cliente: str, documento: str, intentos: int,
                       error: dict = None) -> dict:
    """Ficha de un documento que agotó los intentos, con su última causa conocida."""
    error = error or {}
    codigo = error.get("error_codigo") or ""
    razon = error.get("error_razon") or ""
    mensaje = mensaje_legible(error.get("error_mensaje"))
    ultimo = error.get("created_at")

    return {
        "key_cli":   key_cli,
        "cliente":   cliente,
        "documento": documento,
        "intentos":  intentos,
        "codigo":    str(codigo),
        "razon":     str(razon),
        "mensaje":   mensaje,
        "causa":     clasificar_causa(codigo, razon, mensaje) if (codigo or razon or mensaje) else CAUSA_TECNICA,
        "ultimo_intento": ultimo.isoformat() if hasattr(ultimo, "isoformat") else (ultimo or ""),
    }


def cufe(result: dict) -> str:
    """CUFE recortado para el log. La API puede devolver null, no solo omitirlo."""
    return (result.get("cufe") or "")[:20]


def detalle_fallo(key_cli: str, cliente: str, documento: str, result: dict) -> dict:
    """Entrada de fallidas_detalle para el correo consolidado.

    La clave 'factura' se conserva por compatibilidad con las entradas que ya
    están en Redis y con el formateo del correo; contiene el identificador del
    documento sea factura, NC o documento de soporte.
    """
    raw = result.get("detail", "")
    if isinstance(raw, list):
        items = [str(d) for d in raw]
    elif isinstance(raw, dict):
        items = [f"{k}: {v}" for k, v in raw.items()]
    elif raw:
        items = [str(raw)]
    else:
        items = []

    codigo = result.get("reasonCode", "")
    return {
        "key_cli": key_cli,
        "cliente": cliente,
        "factura": documento,
        "codigo":  str(codigo) if codigo else "",
        "razon":   str(result.get("reason", "") or ""),
        "mensaje": str(result.get("message", "") or ""),
        "detalle": items,
    }


def error_mensaje(result: dict) -> str:
    """Payload JSON que se guarda en la columna error_mensaje."""
    return json.dumps(
        {
            "message": str(result.get("message", "")),
            "detail":  str(result.get("detail", "")),
        },
        ensure_ascii=False,
    )


async def registrar_error(pool, insertar, **campos) -> None:
    """Guarda el error del intento sin dejar que un fallo del propio log tumbe
    la corrida.

    Antes la excepción del INSERT subía hasta el handler de conexión del
    cliente, que devolvía total=0 y marcaba al cliente como inalcanzable: se
    perdían todos los envíos ya realizados y el diagnóstico era falso.
    """
    try:
        async with pool.acquire() as conn:
            await insertar(conn, **campos)
    except Exception as exc:
        print(f"    [WARN] no se pudo registrar el error en la DB: {exc}", file=sys.stderr)


def resumen(
    key_cli: str,
    nombre: str,
    *,
    total: int = 0,
    exitosas: int = 0,
    fallidas: int = 0,
    omitidas: int = 0,
    ya_emitidas: int = 0,
    agotadas: int = 0,
    fallidas_detalle: list = None,
    agotadas_detalle: list = None,
    connection_error: str = None,
) -> dict:
    """Resultado de un (cliente, tipo). Misma forma para los tres flujos.

    'agotadas' son documentos que ya gastaron MAX_INTENTOS: nadie los va a
    reintentar. Antes desaparecían del reporte y quedaban atascados en silencio.
    """
    s = {
        'key_cli':     key_cli,
        'nombre':      nombre,
        'total':       total,
        'exitosas':    exitosas,
        'fallidas':    fallidas,
        'omitidas':    omitidas,
        'ya_emitidas': ya_emitidas,
        'agotadas':    agotadas,
    }
    if fallidas_detalle:
        s['fallidas_detalle'] = fallidas_detalle
    if agotadas_detalle:
        s['agotadas_detalle'] = agotadas_detalle
    if connection_error:
        s['connection_error'] = connection_error
    return s
