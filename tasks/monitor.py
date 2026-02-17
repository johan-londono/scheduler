import os
import logging
from datetime import datetime

import requests

from app import celery_app

logger = logging.getLogger(__name__)


def _verificar_endpoint(nombre, url, timeout=10):
    """Realiza un GET a la URL y retorna el estado."""
    try:
        resp = requests.get(url, timeout=timeout)
        return {
            "proceso": nombre,
            "estado": "OK" if resp.status_code < 500 else "ERROR",
            "detalle": f"HTTP {resp.status_code} en {resp.elapsed.total_seconds():.2f}s",
        }
    except requests.ConnectionError:
        return {"proceso": nombre, "estado": "ERROR", "detalle": "Sin conexión"}
    except requests.Timeout:
        return {"proceso": nombre, "estado": "ERROR", "detalle": f"Timeout (>{timeout}s)"}
    except Exception as e:
        return {"proceso": nombre, "estado": "ERROR", "detalle": str(e)[:200]}


@celery_app.task(name="tasks.monitor.verificar_apis")
def verificar_apis():
    """Verifica el estado de las APIs de Siigo y Dominus y envía correo con el resultado."""
    from tasks.correo import enviar_correo

    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    dominus_api_url = os.environ.get("DOMINUS_API_URL", "http://54.175.22.139:8001")

    logger.info(f"[{ahora}] Verificando estado de APIs...")

    resultados = [
        _verificar_endpoint("API Dominus", dominus_api_url),
    ]

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
        datos_reporte={
            "customer_id": "N/A",
            "fecha_inicio": ahora,
            "fecha_fin": ahora,
            "resultados": resultados,
        },
    )

    return resumen
