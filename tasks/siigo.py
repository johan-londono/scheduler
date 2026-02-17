import os
import logging
import subprocess
from datetime import date

from app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.siigo.sincronizar_siigo")
def sincronizar_siigo(customer_id=23, procesos=None):
    """Ejecuta la sincronización de datos desde Siigo para el mes actual."""
    from tasks.correo import enviar_correo

    if procesos is None:
        procesos = ["invoices", "customers", "products"]

    script_path = os.environ.get("EREPORTS_SIIGO_SCRIPT_PATH", "")
    if not script_path:
        logger.error("EREPORTS_SIIGO_SCRIPT_PATH no está configurado.")
        return "Error: EREPORTS_SIIGO_SCRIPT_PATH no configurado"

    script_file = os.path.join(script_path, "script.py")
    venv_python = os.path.join(script_path, ".venv", "bin", "python3")
    python_bin = venv_python if os.path.isfile(venv_python) else "python3"

    hoy = date.today()
    fecha_inicio = hoy.replace(day=1).strftime("%Y-%m-%d")
    fecha_fin = hoy.strftime("%Y-%m-%d")

    resultados = []

    for proceso in procesos:
        comando = [
            python_bin, script_file,
            "--customer", str(customer_id),
            "-ds", fecha_inicio,
            "-de", fecha_fin,
            "-ps", proceso,
        ]

        logger.info(f"Ejecutando sincronización Siigo: customer={customer_id} proceso={proceso} rango={fecha_inicio} a {fecha_fin}")

        resultado = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            cwd=script_path,
            timeout=600,
        )

        if resultado.returncode == 0:
            logger.info(f"Sincronización {proceso} completada exitosamente.")
            resultados.append(f"{proceso}: OK")
        else:
            logger.error(f"Error en sincronización {proceso}: {resultado.stderr}")
            resultados.append(f"{proceso}: ERROR - {resultado.stderr[:200]}")

    resumen = f"Sincronización Siigo customer={customer_id} [{fecha_inicio} a {fecha_fin}]:\n" + "\n".join(f"  - {r}" for r in resultados)
    logger.info(resumen)

    enviar_correo.delay(
        asunto=f"Sincronización Siigo - Customer {customer_id}",
        mensaje=resumen,
    )

    return resumen
