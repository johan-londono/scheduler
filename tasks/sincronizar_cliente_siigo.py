import os
import logging
import subprocess
from datetime import date

from app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.siigo.sincronizar_siigo")
def sincronizar_siigo(customer_id=23, procesos=None, destinatarios=None, env_config=None):
    """Ejecuta la sincronización de datos desde Siigo para el mes actual."""
    from tasks.envio_correo import enviar_correo

    if procesos is None:
        procesos = ["invoices", "customers", "products", "users", "credit-notes"]

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script_file = os.path.join(project_dir, "scripts", "siigo_script.py")
    venv_python = os.path.join(project_dir, ".venv", "bin", "python3")
    python_bin = venv_python if os.path.isfile(venv_python) else "python3"

    # Construir entorno para el subprocess: hereda el env actual y aplica
    # los overrides de env_config (credenciales API y DB desde la tabla).
    env_subprocess = os.environ.copy()
    if env_config:
        env_subprocess.update({k: str(v) for k, v in env_config.items()})

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
            cwd=os.path.dirname(script_file),
            timeout=600,
            env=env_subprocess,
        )

        if resultado.returncode == 0:
            logger.info(f"Sincronización {proceso} completada exitosamente.")
            resultados.append({"proceso": proceso, "estado": "OK", "detalle": ""})
        else:
            error_msg = resultado.stderr[:200]
            logger.error(f"Error en sincronización {proceso}: {error_msg}")
            resultados.append({"proceso": proceso, "estado": "ERROR", "detalle": error_msg})

    resumen_texto = f"Sincronización Siigo customer={customer_id} [{fecha_inicio} a {fecha_fin}]:\n" + "\n".join(
        f"  - {r['proceso']}: {r['estado']}" for r in resultados
    )
    logger.info(resumen_texto)

    enviar_correo.delay(
        asunto=f"Sincronización Siigo - Customer {customer_id}",
        mensaje=resumen_texto,
        destinatarios=destinatarios,
        datos_reporte={
            "customer_id": customer_id,
            "fecha_inicio": fecha_inicio,
            "fecha_fin": fecha_fin,
            "resultados": resultados,
        },
    )

    return resumen_texto
