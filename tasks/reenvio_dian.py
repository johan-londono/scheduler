"""
tasks/reenvio_dian.py — Reenvío automático de documentos electrónicos a la DIAN
================================================================================

Tarea Celery configurable para reenviar facturas, notas crédito y documentos
de soporte pendientes de aceptación por la DIAN.

Arquitectura
------------
El scheduler invoca como subprocess el módulo ``reenvio_service.main`` de
``scripts/``, pasando ``--tipo`` para seleccionar el tipo
de documento.  El script procesa todos los clientes con ``transmitir=true``
(o solo el indicado en ``key_cli``) y emite al final de su stdout la línea::

    RESUMEN_JSON:{...}

que este módulo parsea y acumula en Redis.  A las 17:00 la tarea
``enviar_reporte_dian_diario`` consolida todas las ejecuciones del día y
envía un único correo de notificación.

Tipos de documento soportados
------------------------------
+-----------------------+----------------+----------------------------------+
| tipo_doc (kwarg)      | --tipo al CLI  | Documentos procesados            |
+=======================+================+==================================+
| ``"facturas"``        | ``facturas``   | Facturas electrónicas (FE)       |
+-----------------------+----------------+----------------------------------+
| ``"notas_credito"``   | ``nc``         | Notas crédito electrónicas (NC)  |
+-----------------------+----------------+----------------------------------+
| ``"documentos_soporte"`` | ``docsoporte`` | Documentos de soporte (DS)    |
+-----------------------+----------------+----------------------------------+

Tareas registradas
------------------
- ``reenviar_documentos_dian``  — ejecuta el reenvío y acumula en Redis
- ``enviar_reporte_dian_diario`` — consolida y envía el correo (17:00)

Alias de compatibilidad
-----------------------
``reenviar_facturas_dian`` y ``reenviar_documentos_soporte_dian`` mantienen
los nombres originales registrados en la DB; ambos delegan a
``reenviar_documentos_dian`` con el ``tipo_doc`` correspondiente.

Extender con un nuevo tipo
--------------------------
1. Agregar la entrada en ``_TIPOS_CLI_DIAN`` (clave → argumento ``--tipo``).
2. Agregar la etiqueta legible en ``_ETIQUETAS_DIAN``.
3. Asegurarse de que ``scripts/reenvio_service/main.py`` acepte
   el nuevo valor en su lista ``TIPOS_VALIDOS``.
4. Crear la entrada en la DB vía ``POST /tasks`` con el nuevo ``tipo_doc``.
"""
import json
import os
import subprocess
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app import celery_app

logger = logging.getLogger(__name__)

# ── Zona horaria y Redis ──────────────────────────────────────────────────────

_TZ               = ZoneInfo("America/Bogota")
_REDIS_KEY_PREFIX = "dian:reporte"
_REDIS_TTL        = 60 * 60 * 48   # 48 horas

# Directorio scripts/ de este proyecto (cwd para correr reenvio_service.main)
_SCRIPTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'scripts')
)

# Argumento --tipo que acepta reenvio_service.main para cada tipo de documento
_TIPOS_CLI_DIAN: dict[str, str] = {
    "facturas":           "facturas",
    "notas_credito":      "nc",
    "documentos_soporte": "docsoporte",
}

# Etiqueta legible para cada tipo (usada en correo y logs)
_ETIQUETAS_DIAN: dict[str, str] = {
    "facturas":           "facturas",
    "notas_credito":      "notas crédito",
    "documentos_soporte": "documentos de soporte",
}


# ── Helpers internos ──────────────────────────────────────────────────────────

def _fecha_hoy() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _get_redis():
    import redis as _redis_lib
    return _redis_lib.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


def _parsear_resumen(stdout: str) -> dict:
    """
    Extrae el resumen estructurado de la línea RESUMEN_JSON impresa por el módulo.
    Fallback a ceros si no se encuentra.
    """
    for linea in stdout.splitlines():
        if linea.startswith("RESUMEN_JSON:"):
            try:
                return json.loads(linea[len("RESUMEN_JSON:"):])
            except Exception:
                break
    return {
        "clientes": 0, "facturas": 0, "exitosas": 0, "fallidas": 0,
        "errores_cx": 0, "fallidas_detalle": [], "errores_conexion": [],
    }


def _formatear_fallo(f: dict) -> str:
    """
    Formatea el detalle de un documento fallido como lista visual para el correo.
    """
    lineas = []
    if f.get('razon'):
        lineas.append(f"Razón: {f['razon']}")
    if f.get('mensaje'):
        lineas.append(f"Mensaje: {f['mensaje']}")
    for item in f.get('detalle', []):
        lineas.append(f"• {item}")
    return "\n".join(lineas)


def _acumular_resultado_dian(resultado, exito: bool, tipo_doc: str, etiqueta: str) -> None:
    """
    Guarda el resultado de una ejecución en Redis para el reporte consolidado
    de las 17:00.  No envía correo inmediato.

    Clave Redis: ``dian:reporte:YYYY-MM-DD``  (lista de entradas JSON, TTL 48h)
    """
    resumen = _parsear_resumen(resultado.stdout)
    entrada = {
        "tipo_doc":  tipo_doc,
        "etiqueta":  etiqueta,
        "exito":     exito,
        "returncode": resultado.returncode,
        "resumen":   resumen,
        "stderr":    resultado.stderr[:300] if not exito and resultado.stderr else "",
        "timestamp": datetime.now(_TZ).isoformat(),
    }
    clave = f"{_REDIS_KEY_PREFIX}:{_fecha_hoy()}"
    try:
        redis = _get_redis()
        redis.rpush(clave, json.dumps(entrada, ensure_ascii=False))
        redis.expire(clave, _REDIS_TTL)
        logger.info(f"Reenvío DIAN [{etiqueta}] acumulado en Redis (clave={clave}).")
    except Exception as exc:
        logger.error(f"No se pudo acumular resultado en Redis: {exc}")


def _ejecutar_reenvio(tipo_doc: str, key_cli: str, env_subprocess: dict) -> subprocess.CompletedProcess:
    """
    Invoca reenvio_service.main con el --tipo correspondiente a tipo_doc.

    Args:
        tipo_doc       — clave en _TIPOS_CLI_DIAN ("facturas", "notas_credito", "documentos_soporte").
        key_cli        — filtro de cliente; None = todos.
        env_subprocess — entorno completo para el subprocess.
    """
    tipo_cli = _TIPOS_CLI_DIAN.get(tipo_doc)
    if not tipo_cli:
        raise ValueError(
            f"tipo_doc inválido: {tipo_doc!r}. "
            f"Opciones: {list(_TIPOS_CLI_DIAN)}"
        )

    # Preferir el venv del scheduler; si no existe, usar python del sistema
    _base = os.path.join(os.path.dirname(__file__), '..')
    for candidato in (
        os.path.join(_base, "venv", "bin", "python3"),
        os.path.join(_base, ".venv", "bin", "python3"),
        "/usr/bin/python3",
    ):
        if os.path.isfile(candidato):
            python_bin = candidato
            break
    else:
        python_bin = "python3"

    comando = [python_bin, "-m", "reenvio_service.main", "--tipo", tipo_cli]
    if key_cli:
        comando += ["--key-cli", key_cli]

    etiqueta = _ETIQUETAS_DIAN[tipo_doc]
    logger.info(
        f"Iniciando reenvío DIAN [{etiqueta}] | key_cli={key_cli or 'todos'} | cwd={_SCRIPTS_DIR}"
    )

    return subprocess.run(
        comando,
        capture_output=True,
        text=True,
        cwd=_SCRIPTS_DIR,
        timeout=1800,
        env=env_subprocess,
    )


# ── Tareas Celery ─────────────────────────────────────────────────────────────

@celery_app.task(name="tasks.reenvio_dian.reenviar_documentos_dian")
def reenviar_documentos_dian(
    tipo_doc: str,
    key_cli: str = None,
    env_config: dict = None,
):
    """
    Ejecuta el reenvío DIAN y acumula el resultado en Redis para el reporte
    consolidado de las 17:00.

    kwargs aceptados:
        tipo_doc   — tipo de documento a procesar:
                       "facturas"            → --tipo facturas
                       "notas_credito"       → --tipo nc
                       "documentos_soporte"  → --tipo docsoporte
        key_cli    — procesar solo este cliente (vacío = todos)
        env_config — variables de entorno inyectadas al subprocess (credenciales)
    """
    env_subprocess = os.environ.copy()
    if env_config:
        env_subprocess.update({k: str(v) for k, v in env_config.items()})

    resultado = _ejecutar_reenvio(tipo_doc, key_cli, env_subprocess)

    if resultado.stdout:
        for linea in resultado.stdout.splitlines():
            logger.info(linea)
    if resultado.stderr:
        for linea in resultado.stderr.splitlines():
            logger.error(linea)

    exito    = resultado.returncode == 0
    etiqueta = _ETIQUETAS_DIAN[tipo_doc]

    if exito:
        logger.info(f"Reenvío DIAN [{etiqueta}] completado. Resultado acumulado para reporte 17:00.")
    else:
        logger.error(f"Reenvío DIAN [{etiqueta}] terminó con código {resultado.returncode}")

    _acumular_resultado_dian(resultado, exito, tipo_doc=tipo_doc, etiqueta=etiqueta)

    return {
        "returncode": resultado.returncode,
        "stdout":     resultado.stdout[-3000:],
        "stderr":     resultado.stderr[-1000:],
    }


@celery_app.task(name="tasks.reenvio_dian.enviar_reporte_dian_diario")
def enviar_reporte_dian_diario(destinatarios: list = None, **_):
    """
    Consolida todas las ejecuciones DIAN del día y envía un único correo.
    Programar a las 17:00.

    kwargs aceptados:
        destinatarios — lista de correos destino; None = usa MAIL_TO del entorno.
    """
    from tasks.envio_correo import enviar_correo

    clave = f"{_REDIS_KEY_PREFIX}:{_fecha_hoy()}"
    try:
        redis     = _get_redis()
        raw_list  = redis.lrange(clave, 0, -1)
    except Exception as exc:
        logger.error(f"No se pudo leer reporte DIAN de Redis: {exc}")
        return

    if not raw_list:
        logger.info("Reporte DIAN diario: sin ejecuciones registradas hoy.")
        return

    entradas = [json.loads(e) for e in raw_list]

    # ── Consolidar por tipo de documento ─────────────────────────────────────
    por_tipo: dict[str, dict] = {}
    for entrada in entradas:
        etiq = entrada["etiqueta"]
        if etiq not in por_tipo:
            por_tipo[etiq] = {
                "exitosas":          0,
                "fallidas":          0,
                "errores_cx":        0,
                "fallos_detalle":    [],
                "errores_conexion":  [],
                "errores_ejecucion": [],
            }
        res = entrada["resumen"]
        por_tipo[etiq]["exitosas"]         += res.get("exitosas", 0)
        por_tipo[etiq]["fallidas"]         += res.get("fallidas", 0)
        por_tipo[etiq]["errores_cx"]       += res.get("errores_cx", 0)
        por_tipo[etiq]["fallos_detalle"]   += res.get("fallidas_detalle", [])
        por_tipo[etiq]["errores_conexion"] += res.get("errores_conexion", [])
        if not entrada["exito"]:
            por_tipo[etiq]["errores_ejecucion"].append({
                "timestamp":  entrada["timestamp"],
                "returncode": entrada["returncode"],
                "stderr":     entrada["stderr"],
            })

    total_exitosas = sum(v["exitosas"]   for v in por_tipo.values())
    total_fallidas = sum(v["fallidas"]   for v in por_tipo.values())
    total_cx       = sum(v["errores_cx"] for v in por_tipo.values())
    hay_errores    = (
        total_fallidas > 0
        or total_cx > 0
        or any(v["errores_ejecucion"] for v in por_tipo.values())
    )
    estado_global = "Con errores" if hay_errores else "Sin errores"

    # ── Tabla de resultados para la plantilla HTML ────────────────────────────
    resultados_email = []
    for etiq, datos in por_tipo.items():
        if datos["exitosas"] > 0:
            resultados_email.append({
                "proceso": f"{etiq.capitalize()} aceptados por la DIAN",
                "estado":  "OK",
                "detalle": f"{datos['exitosas']} procesado(s) exitosamente",
            })
        for cx in datos["errores_conexion"]:
            resultados_email.append({
                "proceso": f"Sin acceso  —  {cx['cliente']}",
                "estado":  "ERROR",
                "detalle": "Falta de permisos de conexión a la base de datos del cliente.",
            })
        for f in datos["fallos_detalle"]:
            resultados_email.append({
                "proceso": f"{f['factura']}  —  {f.get('cliente', '')}",
                "estado":  "ERROR",
                "detalle": f"[{f['codigo']}]\n{_formatear_fallo(f)}",
            })
        for err in datos["errores_ejecucion"]:
            resultados_email.append({
                "proceso": f"Error de ejecución — {etiq}",
                "estado":  "ERROR",
                "detalle": f"returncode={err['returncode']}  ({err['timestamp']})\n{err['stderr']}",
            })

    # ── Cuerpo en texto plano ─────────────────────────────────────────────────
    fecha_label = datetime.now(_TZ).strftime("%d/%m/%Y")
    mensaje = f"Reporte consolidado DIAN — {fecha_label}\n\n"
    for etiq, datos in por_tipo.items():
        mensaje += (
            f"{etiq.capitalize()}\n"
            f"  Exitosos   : {datos['exitosas']}\n"
            f"  Fallidos   : {datos['fallidas']}\n"
        )
        if datos["errores_cx"]:
            mensaje += f"  Errores cx : {datos['errores_cx']}\n"
            for cx in datos["errores_conexion"]:
                mensaje += f"    {cx['cliente']}: Falta de permisos de conexión a la base de datos.\n"
        if datos["fallos_detalle"]:
            mensaje += f"\n  Detalle de {etiq} fallidos:\n"
            for f in datos["fallos_detalle"]:
                mensaje += f"\n    {f['factura']}  ({f.get('cliente', '')})  [{f['codigo']}]\n"
                for linea in _formatear_fallo(f).splitlines():
                    mensaje += f"      {linea}\n"
        if datos["errores_ejecucion"]:
            for err in datos["errores_ejecucion"]:
                mensaje += f"\n  Error de ejecución ({err['timestamp']}): code={err['returncode']}\n"
                if err["stderr"]:
                    mensaje += f"  {err['stderr']}\n"
        mensaje += "\n"

    mensaje += f"Total exitosos : {total_exitosas}\n"
    mensaje += f"Total fallidos : {total_fallidas}\n"
    if total_cx:
        mensaje += f"Errores de cx  : {total_cx}\n"

    # ── Envío y limpieza ──────────────────────────────────────────────────────
    enviar_correo.delay(
        asunto=f"Reporte DIAN — {fecha_label} — {estado_global}",
        mensaje=mensaje,
        datos_reporte={"resultados": resultados_email},
        destinatarios=destinatarios,
    )
    logger.info(
        f"Reporte DIAN diario enviado ({len(entradas)} ejecución(es) consolidadas, "
        f"tipos: {list(por_tipo)})."
    )

    try:
        _get_redis().delete(clave)
    except Exception:
        pass


# ── Alias para compatibilidad con entradas existentes en la DB ────────────────

@celery_app.task(name="tasks.reenvio_dian.reenviar_facturas_dian")
def reenviar_facturas_dian(key_cli: str = None, env_config: dict = None, **_):
    """Alias de reenviar_documentos_dian con tipo_doc='facturas'."""
    return reenviar_documentos_dian(
        tipo_doc="facturas",
        key_cli=key_cli,
        env_config=env_config,
    )


@celery_app.task(name="tasks.reenvio_dian.reenviar_documentos_soporte_dian")
def reenviar_documentos_soporte_dian(key_cli: str = None, env_config: dict = None, **_):
    """Alias de reenviar_documentos_dian con tipo_doc='documentos_soporte'."""
    return reenviar_documentos_dian(
        tipo_doc="documentos_soporte",
        key_cli=key_cli,
        env_config=env_config,
    )
