"""Check de reenvio.comun. Ejecutar: python test/test_reenvio_comun.py

Cubre lo que antes estaba triplicado y divergió: la clasificación de la
respuesta, el CUFE nulo y la traducción de errores HTTP.
"""

import asyncio
import os


# config.py lee estas variables al importarse
os.environ.setdefault("MAIN_DB_HOST", "x")
os.environ.setdefault("MAIN_DB_NAME", "x")
os.environ.setdefault("MAIN_DB_USER", "x")
os.environ.setdefault("MAIN_DB_PASSWORD", "x")
os.environ.setdefault("MAIN_DB_PORT", "5432")
os.environ.setdefault("API_PYTHON_URL", "https://api.test")
os.environ.setdefault("API_PYTHON_USERNAME", "u")
os.environ.setdefault("API_PYTHON_PASSWORD", "p")

import httpx  # noqa: E402

from reenvio.comun import (  # noqa: E402
    clasificar,
    clasificar_causa,
    cufe,
    detalle_fallo,
    documento_atascado,
    llamar_api,
    mensaje_legible,
    resumen,
)


def test_clasificar():
    assert clasificar({"succeeded": True}) == "exito"
    assert clasificar({"succeeded": False, "reasonCode": "ALREADY_EMITTED"}) == "ya_emitido"
    assert clasificar({"succeeded": False, "reasonCode": "TRANSMISSION_DISABLED"}) == "omitido"
    assert clasificar({"succeeded": False, "reasonCode": "DIAN_REJECTED"}) == "fallo"
    assert clasificar({}) == "fallo"

    # ALREADY_EMITTED ya no se cuenta como envío nuevo en ningún tipo
    assert clasificar({"succeeded": False, "reasonCode": "ALREADY_EMITTED"}) != "exito"
    print("OK clasificar")


def test_cufe_nulo():
    # La API puede devolver la clave con null; antes esto lanzaba TypeError y
    # se perdían todos los conteos del cliente.
    assert cufe({"cufe": None}) == ""
    assert cufe({}) == ""
    assert cufe({"cufe": "a" * 40}) == "a" * 20
    print("OK cufe")


def test_detalle_fallo():
    d = detalle_fallo("k1", "Cliente Uno", "FE-123", {
        "reasonCode": "DIAN_REJECTED",
        "reason": "Rechazado",
        "message": "regla 12",
        "detail": ["campo x", "campo y"],
    })
    assert d["factura"] == "FE-123" and d["cliente"] == "Cliente Uno"
    assert d["detalle"] == ["campo x", "campo y"]

    # detail puede venir como dict o como string suelto
    assert detalle_fallo("k", "c", "d", {"detail": {"a": 1}})["detalle"] == ["a: 1"]
    assert detalle_fallo("k", "c", "d", {"detail": "texto"})["detalle"] == ["texto"]
    assert detalle_fallo("k", "c", "d", {})["detalle"] == []
    print("OK detalle_fallo")


def test_resumen():
    r = resumen("k", "n", total=5, exitosas=2, fallidas=1, ya_emitidas=1, agotadas=1)
    assert r["ya_emitidas"] == 1 and r["agotadas"] == 1
    assert "fallidas_detalle" not in r, "no debe aparecer vacío"
    assert "connection_error" not in r
    print("OK resumen")


def test_clasificar_causa():
    """Códigos reales sacados de dianenvio_errores* en producción."""
    # Falta un dato en la DB del cliente
    assert clasificar_causa("NC_NO_SERIAL", "Missing serial",
                            "La nota crédito no tiene resolución (serial_id) asignada") == "datos"
    assert clasificar_causa("404", "PROVIDER_NOT_FOUND", "") == "datos"

    # Rechazo de la DIAN
    assert clasificar_causa("137", "Error en validaciones Dian", "Campo 'tax' inválido") == "dian"
    assert clasificar_causa("171", "No es posible la transmisión de una nota crédito", "") == "dian"

    # La trampa: código de validación DIAN pero mensaje transitorio. Clasificar
    # solo por código lo marcaría como rechazo y nadie lo reintentaría nunca.
    assert clasificar_causa("137", "Error en validaciones Dian",
                            "Intente más tarde, Documento en proceso") == "tecnica"

    # Errores de infraestructura
    assert clasificar_causa("HTTP_500", "Error HTTP 500", "") == "tecnica"
    assert clasificar_causa("UNEXPECTED_ERROR", "Excepción no controlada", "boom") == "tecnica"
    print("OK clasificar_causa")


def test_documento_atascado():
    ficha = documento_atascado("k1", "Cliente Uno", "NC 45", 3, {
        "error_codigo": "NC_NO_SERIAL",
        "error_razon": "Missing serial",
        "error_mensaje": '{"message": "La NC no tiene resolución", "detail": "NC ID 1091"}',
    })
    assert ficha["causa"] == "datos", ficha
    assert ficha["documento"] == "NC 45" and ficha["intentos"] == 3
    assert "no tiene resolución" in ficha["mensaje"] and "1091" in ficha["mensaje"]

    # Sin error registrado: no se puede culpar a la DIAN de algo que no consta
    assert documento_atascado("k", "c", "FE 1", 3, None)["causa"] == "tecnica"

    # detail vacío no ensucia el mensaje
    assert mensaje_legible('{"message": "solo esto", "detail": "[]"}') == "solo esto"
    print("OK documento_atascado")


async def _llamar(handler):
    original = httpx.AsyncClient

    def mock(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    httpx.AsyncClient = mock
    try:
        return await llamar_api("invoice/getcufe", "tok", {"key_cli": "k"})
    finally:
        httpx.AsyncClient = original


def test_llamar_api():
    ok = asyncio.run(_llamar(lambda req: httpx.Response(200, json={"succeeded": True, "cufe": "abc"})))
    assert ok["succeeded"] is True

    # Error con detail estructurado → se conserva el reasonCode de la API
    detallado = asyncio.run(_llamar(lambda req: httpx.Response(
        422, json={"detail": {"reasonCode": "DIAN_REJECTED", "reason": "r", "message": "m"}}
    )))
    assert detallado["succeeded"] is False and detallado["reasonCode"] == "DIAN_REJECTED"

    # Error sin cuerpo útil → se conserva el texto para poder diagnosticar
    crudo = asyncio.run(_llamar(lambda req: httpx.Response(502, text="bad gateway")))
    assert crudo["reasonCode"] == "HTTP_502" and "bad gateway" in crudo["message"]

    # La URL se arma con el payload en base64url
    vistas = []

    def handler(req):
        vistas.append(str(req.url))
        return httpx.Response(200, json={"succeeded": True})

    asyncio.run(_llamar(handler))
    assert vistas[0].startswith("https://api.test/api/v1/invoice/getcufe/"), vistas
    print("OK llamar_api")


if __name__ == "__main__":
    test_clasificar()
    test_cufe_nulo()
    test_detalle_fallo()
    test_resumen()
    test_llamar_api()
    test_clasificar_causa()
    test_documento_atascado()
