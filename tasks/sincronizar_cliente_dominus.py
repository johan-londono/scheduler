import os
import json
import logging
import subprocess
from datetime import date, timedelta

from app import celery_app

logger = logging.getLogger(__name__)


def _ejecutar_dominus(customer_id, branch_id, procesos, fecha_inicio, fecha_fin, timeout=900, destinatarios=None, env_config=None):
    """Lógica compartida para ejecutar el script de Dominus y parsear resultados."""
    from tasks.envio_correo import enviar_correo

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script_file = os.path.join(project_dir, "scripts", "dominus_script.py")
    venv_python = os.path.join(project_dir, ".venv", "bin", "python3")
    python_bin = venv_python if os.path.isfile(venv_python) else "python3"

    # Construir entorno para el subprocess: hereda el env actual y aplica
    # los overrides de env_config (credenciales API Dominus desde la tabla).
    env_subprocess = os.environ.copy()
    if env_config:
        env_subprocess.update({k: str(v) for k, v in env_config.items()})

    resultados = []

    for proceso in procesos:
        comando = [
            python_bin, script_file,
            "--customer", str(customer_id),
            "--branch", str(branch_id),
            "-ds", fecha_inicio,
            "-de", fecha_fin,
            "-ps", proceso,
        ]

        logger.info(
            f"Ejecutando sincronización Dominus: customer={customer_id} "
            f"branch={branch_id} proceso={proceso} rango={fecha_inicio} a {fecha_fin}"
        )

        try:
            resultado = subprocess.run(
                comando,
                capture_output=True,
                text=True,
                cwd=os.path.dirname(script_file),
                timeout=timeout,
                env=env_subprocess,
            )

            if resultado.returncode == 0:
                logger.info(f"Sincronización {proceso} completada exitosamente.")
                detalle = ""
                try:
                    resumen = json.loads(resultado.stdout.strip().split("\n")[-1])
                    info = resumen.get(proceso, {})
                    total = info.get("respuesta", {}).get("detail", {}).get("total_results", "")
                    tiempo = info.get("tiempo", "")
                    detalle = f"{total} registros en {tiempo}s" if total else ""
                except (json.JSONDecodeError, IndexError, KeyError):
                    pass
                resultados.append({"proceso": proceso, "estado": "OK", "detalle": detalle})
            else:
                error_msg = resultado.stderr[-300:] if resultado.stderr else "Sin detalle"
                logger.error(f"Error en sincronización {proceso}: {error_msg}")
                resultados.append({"proceso": proceso, "estado": "ERROR", "detalle": error_msg[:200]})

        except subprocess.TimeoutExpired:
            logger.error(f"Timeout en sincronización {proceso} (>{timeout}s)")
            resultados.append({"proceso": proceso, "estado": "ERROR", "detalle": f"Timeout (>{timeout}s)"})

    resumen_texto = (
        f"Sincronización Dominus customer={customer_id} branch={branch_id} "
        f"[{fecha_inicio} a {fecha_fin}]:\n"
        + "\n".join(f"  - {r['proceso']}: {r['estado']}" for r in resultados)
    )
    logger.info(resumen_texto)

    enviar_correo.delay(
        asunto=f"Sincronización Dominus - Customer {customer_id}",
        mensaje=resumen_texto,
        destinatarios=destinatarios,
        datos_reporte={
            "customer_id": customer_id,
            "branch_id": branch_id,
            "fecha_inicio": fecha_inicio,
            "fecha_fin": fecha_fin,
            "resultados": resultados,
        },
    )

    return resumen_texto


@celery_app.task(name="tasks.dominus.sincronizar_dominus")
def sincronizar_dominus(customer_id=26, branch_id=1054, procesos=None, destinatarios=None, env_config=None):
    """Ejecuta la sincronización de datos desde Dominus para el día anterior."""
    if procesos is None:
        procesos = ["invoices", "consolidated"]

    ayer = date.today() - timedelta(days=1)
    fecha = ayer.strftime("%Y-%m-%d")

    return _ejecutar_dominus(customer_id, branch_id, procesos, fecha, fecha, timeout=900, destinatarios=destinatarios, env_config=env_config)


@celery_app.task(name="tasks.dominus.sincronizar_dominus_mensual")
def sincronizar_dominus_mensual(customer_id=26, branch_id=1054, procesos=None, destinatarios=None, env_config=None):
    """Ejecuta la sincronización completa del mes anterior (1ro de cada mes a las 2AM)."""
    if procesos is None:
        procesos = ["invoices", "consolidated"]

    hoy = date.today()
    # Último día del mes anterior
    ultimo_dia_mes_anterior = hoy.replace(day=1) - timedelta(days=1)
    # Primer día del mes anterior
    primer_dia_mes_anterior = ultimo_dia_mes_anterior.replace(day=1)

    fecha_inicio = primer_dia_mes_anterior.strftime("%Y-%m-%d")
    fecha_fin = ultimo_dia_mes_anterior.strftime("%Y-%m-%d")

    # Timeout más alto para sincronización mensual completa (2 horas)
    return _ejecutar_dominus(customer_id, branch_id, procesos, fecha_inicio, fecha_fin, timeout=7200, destinatarios=destinatarios, env_config=env_config)
