"""Check del rango de fechas de la tarea Siigo. Ejecutar: python test_sincronizar_siigo.py"""

from datetime import date

from tasks.siigo import rango_fechas


def test_rango_fechas():
    # Mes en curso: del dia 1 a hoy
    assert rango_fechas(hoy=date(2026, 7, 29)) == ("2026-07-01", "2026-07-29")

    # Mes anterior: completo, hasta el ultimo dia
    assert rango_fechas(True, hoy=date(2026, 7, 29)) == ("2026-06-01", "2026-06-30")

    # Cruce de anio: enero mira a diciembre del anterior
    assert rango_fechas(True, hoy=date(2026, 1, 15)) == ("2025-12-01", "2025-12-31")

    # Febrero bisiesto
    assert rango_fechas(True, hoy=date(2024, 3, 5)) == ("2024-02-01", "2024-02-29")

    print("OK rango_fechas")


def test_reporte_correo():
    """Un proceso caido no aborta el resto y el correo recibe todos los estados."""
    import tasks.correo
    from tasks import siigo as mod

    enviados = []

    class SyncFalso:
        def __init__(self, **kwargs):
            pass

        async def run_sync(self, date_start, date_end, process=None, **kwargs):
            if process == "users":
                raise RuntimeError("token invalido")
            return {
                "ok": [] if process == "products" else [process],
                "failed": [process] if process == "products" else [],
                "queued": 2,
                "elapsed": 1.5,
            }

    original_sync, original_delay = mod.SiigoSync, tasks.correo.enviar_correo.delay
    mod.SiigoSync = SyncFalso
    tasks.correo.enviar_correo.delay = lambda **kw: enviados.append(kw)
    try:
        mod.sincronizar_siigo(
            customer_id=23,
            siigo_username="u@e.com",
            siigo_access_key="k",
            procesos=["invoices", "products", "users"],
            env_config={"API_SIIGO_URL": "https://api.test"},
        )
    finally:
        mod.SiigoSync = original_sync
        tasks.correo.enviar_correo.delay = original_delay

    assert len(enviados) == 1, "debe enviarse exactamente un correo"
    resultados = enviados[0]["datos_reporte"]["resultados"]
    assert [r["estado"] for r in resultados] == ["OK", "ERROR", "ERROR"], resultados
    assert "1.5s" in resultados[0]["detalle"], resultados[0]
    assert "token invalido" in resultados[2]["detalle"], resultados[2]
    assert enviados[0]["datos_reporte"]["customer_id"] == 23

    print("OK reporte_correo")


if __name__ == "__main__":
    test_rango_fechas()
    test_reporte_correo()
