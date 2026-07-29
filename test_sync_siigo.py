"""Check mínimo de SiigoSync: paginado, encolado y propagación de config.

    python test_sync_siigo.py
"""

import asyncio
import httpx

from scripts.sync_siigo import Config, SiigoSync


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

    generate = [u for u in vistas if "generate/customers" in u]
    assert len(generate) == 3, f"esperadas 3 páginas, hubo {len(generate)}"
    assert all(u.startswith("https://api.test/") for u in vistas), vistas
    assert any("clear=true" in u for u in generate), "página 1 debe limpiar"
    assert sum("clear=false" in u for u in generate) == 2, "páginas 2-3 no deben limpiar"

    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
