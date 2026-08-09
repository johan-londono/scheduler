"""Sincronizacion de modulos Siigo con reporte por correo."""

import calendar
import logging
from datetime import date
import asyncio

from app import celery_app, ahora
from etl.siigo import Config, SiigoSync


logger = logging.getLogger(__name__)

PROCESOS_DEFAULT = ["invoices", "customers", "products", "users", "credit-notes"]


def rango_fechas(mes_anterior=False, hoy=None):
    """Retorna (inicio, fin) como YYYY-MM-DD: mes en curso o mes anterior completo."""
    hoy = hoy or ahora().date()
    if not mes_anterior:
        return hoy.replace(day=1).isoformat(), hoy.isoformat()

    anio, mes = (hoy.year, hoy.month - 1) if hoy.month > 1 else (hoy.year - 1, 12)
    ultimo_dia = calendar.monthrange(anio, mes)[1]
    return date(anio, mes, 1).isoformat(), date(anio, mes, ultimo_dia).isoformat()


@celery_app.task(name="tasks.siigo.sincronizar_siigo")
def sincronizar_siigo(customer_id, siigo_username=None, siigo_access_key=None, procesos=None,
                      destinatarios=None, env_config=None, plantilla=None, mes_anterior=False):
    """Sincroniza cada modulo Siigo y envia un correo con el resumen."""
    procesos = procesos or PROCESOS_DEFAULT
    env_config = env_config or {}

    username = siigo_username or env_config.get("SIIGO_USERNAME")
    access_key = siigo_access_key or env_config.get("SIIGO_ACCESS_KEY")
    fecha_inicio, fecha_fin = rango_fechas(mes_anterior)

    # Credenciales incompletas es el fallo más probable y antes reventaba aquí,
    # antes de la primera línea de reporte: la tarea moría sin enviar correo.
    try:
        sync_client = SiigoSync(
            customer=customer_id,
            config=Config(
                api_url=env_config.get("API_SIIGO_URL"),
                api_user=env_config.get("API_SIIGO_USER"),
                api_password=env_config.get("API_SIIGO_PASSWORD"),
                # Ajustables desde scheduler_credentials sin desplegar: cuando la
                # API se pone inestable el número de reintentos es lo primero
                # que hay que poder subir.
                intentos=int(env_config.get("SIIGO_INTENTOS", 3)),
                espera_reintento=int(env_config.get("SIIGO_ESPERA_REINTENTO", 2)),
            ),
            username=username,
            access_key=access_key,
        )
    except ValueError as error:
        logger.error(f"Configuración Siigo inválida: {error}")
        _reportar(customer_id, fecha_inicio, fecha_fin, destinatarios, plantilla, [
            {"proceso": p, "estado": "ERROR", "detalle": f"Configuración inválida: {error}"}
            for p in procesos
        ])
        raise

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
        fallidas = data.get("paginas_fallidas", {}).get(proceso, 0)
        paginas_ok = data.get("paginas_ok", data.get("queued", 0))

        if ok:
            detalle = f"{paginas_ok} pagina(s) adicional(es) - {data['elapsed']:.1f}s"
        elif fallidas:
            detalle = f"{fallidas} de {data.get('queued', 0)} pagina(s) adicional(es) fallaron"
        else:
            detalle = "la API no devolvio datos validos"

        resultados.append({
            "proceso": proceso,
            "estado": "OK" if ok else "ERROR",
            "detalle": detalle,
        })

    return _reportar(customer_id, fecha_inicio, fecha_fin, destinatarios, plantilla, resultados)


def _reportar(customer_id, fecha_inicio, fecha_fin, destinatarios, plantilla, resultados) -> str:
    """Arma el resumen y encola el correo. Un solo camino de salida."""
    from tasks.correo import enviar_correo

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
