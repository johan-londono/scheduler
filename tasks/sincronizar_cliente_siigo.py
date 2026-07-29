"""Sincronizacion de modulos Siigo con reporte por correo."""

import calendar
import logging
from datetime import date
import asyncio

from app import celery_app
from scripts.sync_siigo import Config, SiigoSync  # ajusta el import real


logger = logging.getLogger(__name__)

PROCESOS_DEFAULT = ["invoices", "customers", "products", "users", "credit-notes"]


def rango_fechas(mes_anterior=False, hoy=None):
    """Retorna (inicio, fin) como YYYY-MM-DD: mes en curso o mes anterior completo."""
    hoy = hoy or date.today()
    if not mes_anterior:
        return hoy.replace(day=1).isoformat(), hoy.isoformat()

    anio, mes = (hoy.year, hoy.month - 1) if hoy.month > 1 else (hoy.year - 1, 12)
    ultimo_dia = calendar.monthrange(anio, mes)[1]
    return date(anio, mes, 1).isoformat(), date(anio, mes, ultimo_dia).isoformat()


@celery_app.task(name="tasks.siigo.sincronizar_siigo")
def sincronizar_siigo(customer_id, siigo_username=None, siigo_access_key=None, procesos=None,
                      destinatarios=None, env_config=None, plantilla=None, mes_anterior=False):
    """Sincroniza cada modulo Siigo y envia un correo con el resumen."""
    from tasks.envio_correo import enviar_correo

    procesos = procesos or PROCESOS_DEFAULT
    env_config = env_config or {}

    username = siigo_username or env_config.get("SIIGO_USERNAME")
    access_key = siigo_access_key or env_config.get("SIIGO_ACCESS_KEY")
    fecha_inicio, fecha_fin = rango_fechas(mes_anterior)

    siigo_config = Config(
        api_url=env_config.get("API_SIIGO_URL"),
        api_user=env_config.get("API_SIIGO_USER"),
        api_password=env_config.get("API_SIIGO_PASSWORD"),
    )

    sync_client = SiigoSync(
        customer=customer_id,
        config=siigo_config,
        username=username,
        access_key=access_key,
    )

    resultados = []

    for proceso in procesos:
        try:
            data = asyncio.run(sync_client.run_sync(
                date_start=fecha_inicio,
                date_end=fecha_fin,
                process=proceso,
            ))
        except Exception as error:
            # Un proceso caido no debe abortar los restantes
            logger.error(f"Error sincronizando {proceso}: {error}", exc_info=True)
            resultados.append({"proceso": proceso, "estado": "ERROR", "detalle": str(error)[:200]})
            continue

        ok = proceso in data["ok"]
        resultados.append({
            "proceso": proceso,
            "estado": "OK" if ok else "ERROR",
            "detalle": (
                f"{data['queued']} pagina(s) adicional(es) - {data['elapsed']:.1f}s"
                if ok else "la API no devolvio datos validos"
            ),
        })

    resumen = (
        f"Sincronizacion Siigo customer={customer_id} [{fecha_inicio} a {fecha_fin}]:\n"
        + "\n".join(f"  - {r['proceso']}: {r['estado']}" for r in resultados)
    )
    logger.info(resumen)

    enviar_correo.delay(
        asunto=f"Sincronizacion Siigo - Customer {customer_id}",
        mensaje=resumen,
        destinatarios=destinatarios,
        plantilla=plantilla,
        datos_reporte={
            "customer_id": customer_id,
            "fecha_inicio": fecha_inicio,
            "fecha_fin": fecha_fin,
            "resultados": resultados,
        },
    )

    return resumen
