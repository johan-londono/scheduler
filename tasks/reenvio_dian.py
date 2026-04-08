"""
tasks/reenvio_dian.py — Reenvío automático de documentos electrónicos a la DIAN
================================================================================

Tarea Celery configurable para reenviar facturas, notas crédito y documentos
de soporte pendientes de aceptación por la DIAN.

Arquitectura
------------
El scheduler invoca como subprocess el módulo ``reenvio_service.main`` de
``scripts/``, una vez por cada combinación (cliente, tipo).  Cada invocación
emite al final de su stdout la línea::

    RESUMEN_JSON:{...}

que este módulo parsea y acumula en Redis.  A las 17:00 la tarea
``enviar_reporte_dian_diario`` consolida todas las ejecuciones del día y
envía un único correo con el desglose por tipo y por cliente.

Tipos de documento soportados
------------------------------
+-----------------------+----------------+----------------------------------+
| tipo_doc              | --tipo al CLI  | Documentos procesados            |
+=======================+================+==================================+
| ``"facturas"``        | ``facturas``   | Facturas electrónicas (FE)       |
+-----------------------+----------------+----------------------------------+
| ``"notas_credito"``   | ``nc``         | Notas crédito electrónicas (NC)  |
+-----------------------+----------------+----------------------------------+
| ``"documentos_soporte"`` | ``docsoporte`` | Documentos de soporte (DS)    |
+-----------------------+----------------+----------------------------------+

Kwargs de la tarea principal (``reenviar_documentos_dian``)
------------------------------------------------------------
tipo_doc   str        Tipo único (retrocompatibilidad). Default: "facturas".
tipos_doc  list[str]  Uno o varios tipos en una sola ejecución.
                      Ej: ["facturas", "notas_credito"]
key_cli    str        Clave de un único cliente. Sin valor = todos.
key_clis   list[str]  Varios clientes en una sola ejecución.
                      Ej: ["abc123", "def456"]
env_config dict       Variables de entorno inyectadas al subprocess.

Ejemplos de kwargs en la DB
----------------------------
# Un cliente, un tipo (uso mínimo)
{"key_cli": "abc123", "tipo_doc": "facturas"}

# Varios clientes, un tipo
{"key_clis": ["abc123", "def456"], "tipo_doc": "facturas"}

# Un cliente, varios tipos
{"key_cli": "abc123", "tipos_doc": ["facturas", "notas_credito"]}

# Varios clientes, varios tipos
{"key_clis": ["abc123", "def456"], "tipos_doc": ["facturas", "notas_credito"]}

# Todos los clientes, todos los tipos (sin key_cli/key_clis)
{"tipos_doc": ["facturas", "notas_credito", "documentos_soporte"]}

Filtros de fecha
----------------
Por defecto se procesan solo documentos del mes en curso.  Se puede
sobrescribir via env_config (o variables de entorno del worker):

  FILTRO_DIA   = YYYY-MM-DD          (día específico)
  FILTRO_MES   = YYYY-MM             (mes específico)
  FILTRO_ANIO  = YYYY                (año completo)
  FILTRO_DESDE + FILTRO_HASTA        (rango personalizado YYYY-MM-DD)

Tareas registradas
------------------
- ``reenviar_documentos_dian``   — ejecuta el reenvío y acumula en Redis
- ``enviar_reporte_dian_diario`` — consolida y envía el correo (17:00)

Alias de compatibilidad
-----------------------
``reenviar_facturas_dian`` delega a ``reenviar_documentos_dian`` aceptando
los mismos kwargs.  Default tipo_doc="facturas" para no romper entradas
existentes en la DB.

``reenviar_documentos_soporte_dian`` es un alias fijo a tipo_doc="documentos_soporte".

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
    tipo_doc: str = None,
    tipos_doc: list = None,
    key_cli: str = None,
    key_clis: list = None,
    env_config: dict = None,
    **_,
):
    """
    Ejecuta el reenvío DIAN y acumula el resultado en Redis para el reporte
    consolidado de las 17:00.

    kwargs aceptados:
        tipos_doc  — lista de tipos a procesar:
                       ["facturas"]
                       ["facturas", "notas_credito"]
                       ["facturas", "notas_credito", "documentos_soporte"]
        tipo_doc   — tipo único (retrocompatibilidad); ignorado si se usa tipos_doc.
        key_clis   — lista de clientes a procesar: ["abc123", "def456"]
        key_cli    — cliente único (retrocompatibilidad); ignorado si se usa key_clis.
                     Sin valor = todos los clientes.
        env_config — variables de entorno inyectadas al subprocess (credenciales)
    """
    tipos   = tipos_doc if tipos_doc else [tipo_doc]
    clientes = key_clis if key_clis else ([key_cli] if key_cli else [None])

    if not tipos or any(t not in _TIPOS_CLI_DIAN for t in tipos):
        invalidos = [t for t in tipos if t not in _TIPOS_CLI_DIAN]
        raise ValueError(
            f"tipo(s) inválido(s): {invalidos}. Opciones: {list(_TIPOS_CLI_DIAN)}"
        )

    env_subprocess = os.environ.copy()
    if env_config:
        env_subprocess.update({k: str(v) for k, v in env_config.items()})

    resultados = []
    for cli in clientes:
        for tipo in tipos:
            etiqueta  = _ETIQUETAS_DIAN[tipo]
            cli_label = cli or "todos"
            logger.info(f"Procesando [{etiqueta}] cliente={cli_label}")

            try:
                resultado = _ejecutar_reenvio(tipo, cli, env_subprocess)
            except Exception as exc:
                logger.error(
                    f"Error inesperado al ejecutar reenvío DIAN "
                    f"[{etiqueta}] cliente={cli_label}: {exc}"
                )
                resultados.append({
                    "tipo":       tipo,
                    "key_cli":    cli_label,
                    "returncode": -1,
                    "error":      str(exc),
                })
                continue

            if resultado.stdout:
                for linea in resultado.stdout.splitlines():
                    logger.info(linea)
            if resultado.stderr:
                for linea in resultado.stderr.splitlines():
                    logger.error(linea)

            exito = resultado.returncode == 0
            if exito:
                logger.info(f"Reenvío DIAN [{etiqueta}] cliente={cli_label} completado.")
            else:
                logger.error(
                    f"Reenvío DIAN [{etiqueta}] cliente={cli_label} "
                    f"terminó con código {resultado.returncode}"
                )

            _acumular_resultado_dian(resultado, exito, tipo_doc=tipo, etiqueta=etiqueta)
            resultados.append({
                "tipo":       tipo,
                "key_cli":    cli_label,
                "returncode": resultado.returncode,
                "stdout":     resultado.stdout[-3000:],
                "stderr":     resultado.stderr[-1000:],
            })

    return resultados if len(resultados) > 1 else resultados[0]


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
                "exitosas":            0,
                "fallidas":            0,
                "errores_cx":          0,
                "fallos_detalle":      [],
                "errores_conexion":    [],
                "errores_ejecucion":   [],
                "clientes_detalle":    [],
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
        # Desglose por cliente: filtrar los que corresponden a este tipo
        for r in res.get("resultados_por_cliente", []):
            if _ETIQUETAS_DIAN.get(r.get("tipo")) == etiq:
                por_tipo[etiq]["clientes_detalle"].append(r)

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
        # Una fila por cliente con sus conteos
        for r in datos["clientes_detalle"]:
            partes = []
            if r["exitosas"]: partes.append(f"{r['exitosas']} exitosa(s)")
            if r["fallidas"]: partes.append(f"{r['fallidas']} fallida(s)")
            if r["omitidas"]: partes.append(f"{r['omitidas']} omitida(s)")
            tiene_fallo = r["fallidas"] > 0
            resultados_email.append({
                "proceso": f"{etiq.capitalize()} — {r['cliente']}",
                "estado":  "ERROR" if tiene_fallo else "OK",
                "detalle": ", ".join(partes) if partes else "Sin documentos procesados",
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
        if datos["clientes_detalle"]:
            mensaje += "  Por cliente:\n"
            for r in datos["clientes_detalle"]:
                partes = []
                if r["exitosas"]: partes.append(f"{r['exitosas']} exitosa(s)")
                if r["fallidas"]: partes.append(f"{r['fallidas']} fallida(s)")
                if r["omitidas"]: partes.append(f"{r['omitidas']} omitida(s)")
                mensaje += f"    {r['cliente']}: {', '.join(partes) if partes else 'sin documentos'}\n"
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
def reenviar_facturas_dian(
    tipo_doc: str = "facturas",
    tipos_doc: list = None,
    key_cli: str = None,
    key_clis: list = None,
    env_config: dict = None,
    **_,
):
    """
    Alias compatible con la DB.  Por defecto procesa facturas.

    kwargs aceptados:
        tipos_doc — lista de tipos: ["facturas", "notas_credito"], etc.
        tipo_doc  — tipo único (retrocompatibilidad, default "facturas").
        key_clis  — lista de clientes: ["abc123", "def456"].
        key_cli   — cliente único (retrocompatibilidad).
    """
    return reenviar_documentos_dian(
        tipo_doc=tipo_doc,
        tipos_doc=tipos_doc,
        key_cli=key_cli,
        key_clis=key_clis,
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
