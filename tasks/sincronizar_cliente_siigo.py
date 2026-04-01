import os
import logging
import subprocess
import calendar
from datetime import date

from app import celery_app

logger = logging.getLogger(__name__)

SYNC_SIIGO_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "sync_siigo.py")


@celery_app.task(name="tasks.siigo.sincronizar_siigo")
def sincronizar_siigo(customer_id=23, procesos=None, destinatarios=None, env_config=None, plantilla=None, mes_anterior=False):
    """Ejecuta la sincronización de datos desde Siigo para el mes actual o el anterior."""
    from tasks.envio_correo import enviar_correo

    if procesos is None:
        procesos = ["invoices", "customers", "products", "users", "credit-notes"]

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_python = os.path.join(project_dir, ".venv", "bin", "python3")
    python_bin = venv_python if os.path.isfile(venv_python) else "python3"

    # Construir entorno para el subprocess: hereda el env actual y aplica
    # los overrides de env_config (credenciales API y Siigo desde la tabla).
    env_subprocess = os.environ.copy()
    if env_config:
        env_subprocess.update({k: str(v) for k, v in env_config.items()})

    api_url = env_subprocess.get("API_SIIGO_URL", "")
    api_user = env_subprocess.get("API_SIIGO_USER", "")
    api_password = env_subprocess.get("API_SIIGO_PASSWORD", "")
    siigo_username = env_subprocess.get("SIIGO_USERNAME", "")
    siigo_access_key = env_subprocess.get("SIIGO_ACCESS_KEY", "")

    hoy = date.today()
    if mes_anterior:
        anio = hoy.year if hoy.month > 1 else hoy.year - 1
        mes = hoy.month - 1 if hoy.month > 1 else 12
        ultimo_dia = calendar.monthrange(anio, mes)[1]
        fecha_inicio = date(anio, mes, 1).strftime("%Y-%m-%d")
        fecha_fin = date(anio, mes, ultimo_dia).strftime("%Y-%m-%d")
    else:
        fecha_inicio = hoy.replace(day=1).strftime("%Y-%m-%d")
        fecha_fin = hoy.strftime("%Y-%m-%d")

    comando = [
        python_bin, SYNC_SIIGO_SCRIPT,
        "--url", api_url,
        "--username", api_user,
        "--password", api_password,
        "--key", str(customer_id),
        "--modules", *procesos,
        "--date-start", fecha_inicio,
        "--date-end", fecha_fin,
    ]

    if siigo_username and siigo_access_key:
        comando += ["--siigo-username", siigo_username, "--siigo-access-key", siigo_access_key]
    else:
        comando.append("--skip-token")

    logger.info(f"Ejecutando sincronización Siigo: customer={customer_id} módulos={procesos} rango={fecha_inicio} a {fecha_fin}")

    resultado = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        timeout=600,
        env=env_subprocess,
    )

    if resultado.returncode == 0:
        logger.info("Sincronización Siigo completada exitosamente.")
        resultados = [{"proceso": p, "estado": "OK", "detalle": ""} for p in procesos]
    else:
        if resultado.stdout:
            logger.error(f"stdout: {resultado.stdout[:500]}")
        if resultado.stderr:
            logger.error(f"stderr: {resultado.stderr[:500]}")
        error_msg = (resultado.stderr or resultado.stdout or "")[:400]
        logger.error(f"Error en sincronización Siigo: {error_msg}")
        resultados = [{"proceso": p, "estado": "ERROR", "detalle": error_msg} for p in procesos]

    resumen_texto = f"Sincronización Siigo customer={customer_id} [{fecha_inicio} a {fecha_fin}]:\n" + "\n".join(
        f"  - {r['proceso']}: {r['estado']}" for r in resultados
    )
    logger.info(resumen_texto)

    enviar_correo.delay(
        asunto=f"Sincronización Siigo - Customer {customer_id}",
        mensaje=resumen_texto,
        destinatarios=destinatarios,
        plantilla=plantilla,
        datos_reporte={
            "customer_id": customer_id,
            "fecha_inicio": fecha_inicio,
            "fecha_fin": fecha_fin,
            "resultados": resultados,
        },
    )

    return resumen_texto
