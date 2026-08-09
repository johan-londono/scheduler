"""Check mínimo de SiigoSync: paginado, encolado y propagación de config.

    python test_sync_siigo.py
"""

import asyncio
import httpx

from etl.siigo import Config, SiigoSync


def _transport(vistas):
    """MockTransport que devuelve 3 páginas y registra cada request."""

    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(str(request.url))
        if request.url.path.endswith("/token/"):
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path.endswith("/generate/token"):
            return httpx.Response(200, json={"message": "sesion-ok"})
        return httpx.Response(200, json={"detail": {"pages": 3, "total_results": 30}})

    return httpx.MockTransport(handler)


def _transport_pagina_rota(vistas):
    """Igual que _transport, pero la página 2 devuelve 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(str(request.url))
        if request.url.path.endswith("/token/"):
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path.endswith("/generate/token"):
            return httpx.Response(200, json={"message": "sesion-ok"})
        if "page=2" in str(request.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"detail": {"pages": 3, "total_results": 30}})

    return httpx.MockTransport(handler)


async def _correr(transport_factory, vistas, **config_extra):
    sync = SiigoSync(
        customer=23,
        config=Config(api_url="https://api.test", api_user="u", api_password="p", workers=2,
                      espera_reintento=0, **config_extra),
        username="user@empresa.com",
        access_key="key",
    )
    original = httpx.AsyncClient

    def cliente_mock(**kwargs):
        kwargs["transport"] = transport_factory(vistas)
        return original(**kwargs)

    httpx.AsyncClient = cliente_mock
    try:
        return await sync.run_sync("2026-07-01", "2026-07-31", process="customers")
    finally:
        httpx.AsyncClient = original


def _transport_500_intermitente(vistas, fallos_iniciales=2):
    """La página 1 devuelve 500 las primeras N veces y luego responde bien.

    Es el caso real: la API rompe la petición con un 500 esporádico
    ("Can't reconnect until invalid transaction is rolled back") y al reintentar
    ya funciona.
    """
    estado = {"fallos": fallos_iniciales}

    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(str(request.url))
        if request.url.path.endswith("/token/"):
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path.endswith("/generate/token"):
            return httpx.Response(200, json={"message": "sesion-ok"})
        if "page=1" in str(request.url) and estado["fallos"] > 0:
            estado["fallos"] -= 1
            return httpx.Response(500, json={"detail": {"error": "invalid transaction"}})
        return httpx.Response(200, json={"detail": {"pages": 3, "total_results": 30}})

    return httpx.MockTransport(handler)


def _transport_500_permanente(vistas):
    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(str(request.url))
        if request.url.path.endswith("/token/"):
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path.endswith("/generate/token"):
            return httpx.Response(200, json={"message": "sesion-ok"})
        return httpx.Response(500, text="boom")

    return httpx.MockTransport(handler)


async def test_500_intermitente_se_reintenta():
    """Un 500 pasajero en la página 1 ya no tumba el proceso entero."""
    vistas = []
    res = await _correr(_transport_500_intermitente, vistas)

    assert res["ok"] == ["customers"], res
    assert res["failed"] == [], res
    assert res["queued"] == 2, "tras recuperarse debe encolar las páginas 2 y 3"

    # 3 intentos de la página 1 (2 fallidos + 1 bueno) y las 2 restantes
    pagina1 = [u for u in vistas if "page=1" in u]
    assert len(pagina1) == 3, f"esperados 3 intentos de la página 1: {pagina1}"
    print("OK 500_intermitente")


async def test_500_permanente_agota_intentos():
    """Si el 500 no se va, se agotan los intentos y el proceso se reporta ERROR."""
    vistas = []
    res = await _correr(_transport_500_permanente, vistas)

    assert res["failed"] == ["customers"], res
    assert res["ok"] == [], res

    pagina1 = [u for u in vistas if "page=1" in u]
    assert len(pagina1) == 3, f"debe parar en 3 intentos, no insistir: {pagina1}"
    print("OK 500_permanente")


async def test_404_no_se_reintenta():
    """Los 4xx no mejoran esperando: una sola petición."""
    vistas = []

    def factory(v):
        def handler(request):
            v.append(str(request.url))
            if request.url.path.endswith("/token/"):
                return httpx.Response(200, json={"access_token": "tok"})
            if request.url.path.endswith("/generate/token"):
                return httpx.Response(200, json={"message": "sesion-ok"})
            return httpx.Response(404, text="no existe")
        return httpx.MockTransport(handler)

    res = await _correr(factory, vistas)

    assert res["failed"] == ["customers"], res
    assert len([u for u in vistas if "page=1" in u]) == 1, vistas
    print("OK 404_sin_reintento")


async def test_pagina_fallida_no_es_exito():
    """Si una página adicional falla, el proceso no puede reportarse como OK."""
    res = await _correr(_transport_pagina_rota, [])

    assert res["failed"] == ["customers"], res
    assert res["ok"] == [], res
    assert res["paginas_fallidas"] == {"customers": 1}, res
    assert res["paginas_ok"] == 1, res
    print("OK pagina_fallida")


async def main():
    vistas = []
    sync = SiigoSync(
        customer=23,
        config=Config(api_url="https://api.test", api_user="u", api_password="p", workers=2),
        username="user@empresa.com",
        access_key="key",
    )

    original = httpx.AsyncClient

    def cliente_mock(**kwargs):
        kwargs["transport"] = _transport(vistas)
        return original(**kwargs)

    httpx.AsyncClient = cliente_mock
    try:
        res = await sync.run_sync("2026-07-01", "2026-07-31", process="customers")
    finally:
        httpx.AsyncClient = original

    assert res["ok"] == ["customers"], res
    assert res["failed"] == [], res
    assert res["queued"] == 2, f"pages=3 debe encolar las páginas 2 y 3, no {res['queued']}"

    assert sum(u.endswith("/token/") for u in vistas) == 1, f"/token/ debe pedirse 1 vez: {vistas}"
    assert sum(u.endswith("/generate/token") for u in vistas) == 1, f"siigo/token debe pedirse 1 vez: {vistas}"

    generate = [u for u in vistas if "generate/customers" in u]
    assert len(generate) == 3, f"esperadas 3 páginas, hubo {len(generate)}"
    assert all(u.startswith("https://api.test/") for u in vistas), vistas
    assert any("clear=true" in u for u in generate), "página 1 debe limpiar"
    assert sum("clear=false" in u for u in generate) == 2, "páginas 2-3 no deben limpiar"

    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_pagina_fallida_no_es_exito())
    asyncio.run(test_500_intermitente_se_reintenta())
    asyncio.run(test_500_permanente_agota_intentos())
    asyncio.run(test_404_no_se_reintenta())
