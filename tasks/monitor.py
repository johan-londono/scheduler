import logging

import requests

from app import celery_app, ahora as reloj

logger = logging.getLogger(__name__)

APIS_DEFAULT = [
    {"name": "DIAN API", "url": "http://18.234.78.132:8000"},
    {"name": "Account API", "url": "http://54.175.22.139:8001"},
]


def _verificar_endpoint(nombre, url, timeout=15):
    """Realiza un GET a la URL y retorna el estado.

    Sano = respuesta < 400. Antes bastaba con cualquier cosa por debajo de 500,
    así que un 404 o un 401 se reportaban como API en buen estado.
    """
    try:
        resp = requests.get(url, timeout=timeout)
        return {
            "proceso": nombre,
            "estado": "OK" if resp.ok else "ERROR",
            "detalle": f"HTTP {resp.status_code} en {resp.elapsed.total_seconds():.2f}s",
        }
    except requests.ConnectionError:
        return {"proceso": nombre, "estado": "ERROR", "detalle": "Sin conexion"}
    except requests.Timeout:
        return {"proceso": nombre, "estado": "ERROR", "detalle": f"Timeout (>{timeout}s)"}
    except Exception as e:
        return {"proceso": nombre, "estado": "ERROR", "detalle": str(e)[:200]}


@celery_app.task(name="tasks.monitor.verificar_apis")
def verificar_apis(apis=None, destinatario="johan.londono@eholding.com.co"):
    """Verifica el estado de multiples APIs y envia correo con el resultado."""
    from tasks.correo import enviar_correo

    if apis is None:
        apis = APIS_DEFAULT

    ahora = reloj().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"[{ahora}] Verificando estado de {len(apis)} APIs...")

    resultados = [_verificar_endpoint(api["name"], api["url"]) for api in apis]

    todas_ok = all(r["estado"] == "OK" for r in resultados)
    estado = "OK" if todas_ok else "ERROR"

    resumen = (
        f"Monitor de APIs [{ahora}]:\n"
        + "\n".join(f"  - {r['proceso']}: {r['estado']} ({r['detalle']})" for r in resultados)
    )
    logger.info(resumen)

    enviar_correo.delay(
        asunto=f"Monitor APIs - {estado}",
        mensaje=resumen,
        destinatarios=[destinatario],
        datos_reporte={
            "customer_id": "Health Check",
            "fecha_inicio": ahora,
            "fecha_fin": ahora,
            "resultados": resultados,
        },
    )

    return resumen
