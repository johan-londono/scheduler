import os
import subprocess
import logging

from app import celery_app

logger = logging.getLogger(__name__)


def _parsear_resumen(stdout: str) -> dict:
    """
    Extrae el resumen estructurado de la línea RESUMEN_JSON impresa por main.py.
    Fallback a ceros si no se encuentra.
    """
    import json as _json
    for linea in stdout.splitlines():
        if linea.startswith("RESUMEN_JSON:"):
            try:
                return _json.loads(linea[len("RESUMEN_JSON:"):])
            except Exception:
                break
    return {
        "clientes": 0, "facturas": 0, "exitosas": 0, "fallidas": 0,
        "errores_cx": 0, "fallidas_detalle": [],
    }


def _formatear_fallo(f: dict) -> str:
    """
    Formatea el detalle de una factura fallida como lista visual para el correo.
    """
    lineas = []
    if f.get('razon'):
        lineas.append(f"Razón: {f['razon']}")
    if f.get('mensaje'):
        lineas.append(f"Mensaje: {f['mensaje']}")
    for item in f.get('detalle', []):
        lineas.append(f"• {item}")
    return "\n".join(lineas)

# Directorio scripts/ del scheduler (donde vive reenvio_service/)
_SCRIPTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'scripts')
)


@celery_app.task(name="tasks.reenvio_dian.reenviar_facturas_dian")
def reenviar_facturas_dian(key_cli: str = None, env_config: dict = None, destinatarios: list = None):
    """
    Ejecuta el reenvío automático de facturas a la DIAN para todos los clientes
    con transmitir=true (o solo para key_cli si se especifica).

    kwargs aceptados:
        key_cli    — procesar solo este cliente (vacío = todos)
        env_config — variables de entorno inyectadas al subprocess (credenciales)
    """
    env_subprocess = os.environ.copy()
    if env_config:
        env_subprocess.update({k: str(v) for k, v in env_config.items()})

    # Usar el python del venv del scheduler si existe, si no el del sistema
    _project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_python = os.path.join(_project_dir, ".venv", "bin", "python3")
    python_bin = venv_python if os.path.isfile(venv_python) else "python3"

    comando = [python_bin, "-m", "reenvio_service.main"]
    if key_cli:
        comando += ["--key-cli", key_cli]

    logger.info(
        f"Iniciando reenvío DIAN | key_cli={key_cli or 'todos'} | cwd={_SCRIPTS_DIR}"
    )

    resultado = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        cwd=_SCRIPTS_DIR,
        timeout=1800,
        env=env_subprocess,
    )

    if resultado.stdout:
        for linea in resultado.stdout.splitlines():
            logger.info(linea)
    if resultado.stderr:
        for linea in resultado.stderr.splitlines():
            logger.error(linea)

    exito = resultado.returncode == 0

    if exito:
        logger.info("Reenvío DIAN completado exitosamente.")
    else:
        logger.error(f"Reenvío DIAN terminó con código {resultado.returncode}")

    # Notificación por correo
    from tasks.envio_correo import enviar_correo

    resumen = _parsear_resumen(resultado.stdout)
    cliente_label = f"cliente {key_cli}" if key_cli else "todos los clientes"

    if exito:
        estado_texto = (
            f"Completado sin errores" if resumen["fallidas"] == 0
            else f"Completado con {resumen['fallidas']} factura(s) fallida(s)"
        )
    else:
        estado_texto = f"Error de ejecución (código {resultado.returncode})"

    fallos     = resumen.get("fallidas_detalle", [])
    errores_cx = resumen.get("errores_conexion", [])

    resultados_email = []

    if resumen["exitosas"] > 0:
        resultados_email.append({
            "proceso": "Facturas aceptadas por la DIAN",
            "estado":  "OK",
            "detalle": f"{resumen['exitosas']} factura(s) procesadas exitosamente",
        })

    for cx in errores_cx:
        resultados_email.append({
            "proceso": f"Sin acceso  —  {cx['cliente']}",
            "estado":  "ERROR",
            "detalle": "Falta de permisos de conexión a la base de datos del cliente.",
        })

    if fallos:
        resultados_email += [
            {
                "proceso": f"{f['factura']}  —  {f.get('cliente', '')}",
                "estado":  "ERROR",
                "detalle": f"[{f['codigo']}]\n{_formatear_fallo(f)}",
            }
            for f in fallos
        ]

    if not resultados_email:
        resultados_email.append({
            "proceso": f"Facturas enviadas ({cliente_label})",
            "estado":  "OK" if exito else "ERROR",
            "detalle": estado_texto,
        })

    datos_reporte = {"resultados": resultados_email}

    mensaje = (
        f"Cliente: {cliente_label}\n"
        f"Clientes procesados : {resumen['clientes']}\n"
        f"Facturas procesadas : {resumen['facturas']}\n"
        f"Exitosas            : {resumen['exitosas']}\n"
        f"Fallidas            : {resumen['fallidas']}\n"
    )
    if errores_cx:
        mensaje += f"Errores de conexión : {len(errores_cx)}\n"
        for cx in errores_cx:
            mensaje += f"  {cx['cliente']}: Falta de permisos de conexión a la base de datos del cliente.\n"
    if fallos:
        mensaje += "\nDetalle de facturas fallidas:\n"
        for f in fallos:
            mensaje += f"\n  {f['factura']}  ({f.get('cliente', '')})  [{f['codigo']}]\n"
            for linea in _formatear_fallo(f).splitlines():
                mensaje += f"    {linea}\n"
    if not exito and resultado.stderr:
        mensaje += f"\nError:\n{resultado.stderr[:500]}"

    enviar_correo.delay(
        asunto=f"Reenvío DIAN — {estado_texto}",
        mensaje=mensaje,
        datos_reporte=datos_reporte,
        destinatarios=destinatarios,
    )

    return {
        "returncode": resultado.returncode,
        "stdout": resultado.stdout[-3000:],
        "stderr": resultado.stderr[-1000:],
    }
