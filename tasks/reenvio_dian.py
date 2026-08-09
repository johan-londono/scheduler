"""
tasks/reenvio_dian.py — Reenvío automático de documentos electrónicos a la DIAN
================================================================================

Tarea Celery configurable para reenviar facturas, notas crédito y documentos
de soporte pendientes de aceptación por la DIAN.

Arquitectura
------------
El scheduler invoca como subprocess el módulo ``reenvio.main`` de
la raiz del proyecto, una vez por cada combinación (cliente, tipo).  Cada invocación
emite al final de su stdout la línea::

    RESUMEN_JSON:{...}

que este módulo parsea y acumula en Redis.  Una vez al día la tarea
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
tipo_doc   str        Tipo único (retrocompatibilidad). Sin default: hay que
                      enviar tipo_doc o tipos_doc. El alias
                      reenviar_facturas_dian sí trae "facturas" por defecto.
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
- ``reenviar_documentos_dian``      — ejecuta el reenvío y acumula en Redis
- ``enviar_reporte_dian_diario``    — consolida y envía el correo (una vez al día)
- ``reportar_documentos_atascados`` — lista los documentos que agotaron los
  intentos, agrupados por causa (faltan datos / rechazo DIAN / técnico)

Documentos atascados
--------------------
Un documento que gasta MAX_INTENTOS deja de aparecer en la consulta de
pendientes: no se reintenta solo nunca más. ``reportar_documentos_atascados``
los saca a la luz con su último error registrado y los clasifica para que cada
uno tenga un responsable:

  datos    — falta información en la DB del cliente (serial, proveedor…)
             → completarla y reiniciar el contador de intentos
  dian     — la DIAN rechazó el documento por una regla de validación
             → corregir el documento
  tecnica  — error de infraestructura o respuesta transitoria
             → reintentable tal cual

La clasificación mira código **y** mensaje: hay códigos de validación DIAN cuyo
mensaje real es "Intente más tarde, Documento en proceso", que es transitorio.
Las reglas viven en ``reenvio/comun.py`` (``clasificar_causa``).

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
3. Asegurarse de que ``reenvio/main.py`` acepte
   el nuevo valor en su lista ``TIPOS_VALIDOS``.
4. Crear la entrada en la DB vía ``POST /tasks`` con el nuevo ``tipo_doc``.
"""
import json
import os
import subprocess
import logging

from app import ahora, celery_app
from tasks.ejecutar import correr_modulo

logger = logging.getLogger(__name__)

# ── Redis ─────────────────────────────────────────────────────────────────────

_REDIS_KEY_PREFIX = "dian:reporte"
_REDIS_TTL        = 60 * 60 * 48   # 48 horas

# Argumento --tipo que acepta reenvio.main para cada tipo de documento
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
    return ahora().strftime("%Y-%m-%d")


def _get_redis():
    import redis as _redis_lib
    return _redis_lib.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


def _parsear_resumen(stdout: str) -> dict:
    """
    Extrae el resumen estructurado de la línea RESUMEN_JSON impresa por el módulo.

    Si no aparece, el subprocess ni siquiera llegó a emitirla: eso es un fallo,
    no una jornada sin novedad. El campo 'error' hace que el correo lo diga en
    vez de reportar ceros como si todo hubiera ido bien.
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
        "error": "El proceso no emitió resumen (murió antes de terminar).",
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


def _acumular_resultado_dian(resultado, exito: bool, tipo_doc: str, etiqueta: str,
                             key_cli: str = "todos") -> None:
    """
    Guarda el resultado de una ejecución en Redis para el reporte consolidado.
    No envía correo inmediato.

    Clave Redis: ``dian:reporte:YYYY-MM-DD``  (lista de entradas JSON, TTL 48h)
    """
    resumen = _parsear_resumen(resultado.stdout)
    entrada = {
        "tipo_doc":  tipo_doc,
        "etiqueta":  etiqueta,
        "key_cli":   key_cli,
        "exito":     exito,
        "returncode": resultado.returncode,
        "resumen":   resumen,
        "stderr":    resultado.stderr[:300] if not exito and resultado.stderr else "",
        "timestamp": ahora().isoformat(),
    }
    clave = f"{_REDIS_KEY_PREFIX}:{_fecha_hoy()}"
    try:
        redis = _get_redis()
        redis.rpush(clave, json.dumps(entrada, ensure_ascii=False))
        redis.expire(clave, _REDIS_TTL)
        logger.info(f"Reenvío DIAN [{etiqueta}] acumulado en Redis (clave={clave}).")
    except Exception as exc:
        logger.error(f"No se pudo acumular resultado en Redis: {exc}")


def _ejecutar_reenvio(tipo_doc: str, key_cli: str, env_config: dict) -> subprocess.CompletedProcess:
    """
    Invoca reenvio.main con el --tipo correspondiente a tipo_doc.

    Args:
        tipo_doc   — clave en _TIPOS_CLI_DIAN ("facturas", "notas_credito", "documentos_soporte").
        key_cli    — filtro de cliente; None = todos.
        env_config — credenciales a inyectar en el entorno del subprocess.
    """
    tipo_cli = _TIPOS_CLI_DIAN.get(tipo_doc)
    if not tipo_cli:
        raise ValueError(
            f"tipo_doc inválido: {tipo_doc!r}. "
            f"Opciones: {list(_TIPOS_CLI_DIAN)}"
        )

    args = ["--tipo", tipo_cli]
    if key_cli:
        args += ["--key-cli", key_cli]

    etiqueta = _ETIQUETAS_DIAN[tipo_doc]
    logger.info(f"Iniciando reenvío DIAN [{etiqueta}] | key_cli={key_cli or 'todos'}")

    return correr_modulo("reenvio.main", args, env_config=env_config, timeout=1800)


def _consolidar(entradas: list[dict]) -> dict:
    """Agrupa las ejecuciones del día por tipo de documento y por cliente.

    Ojo con la naturaleza de cada cifra:
      - exitosas/fallidas/ya_emitidas/omitidas son *deltas* de cada corrida → se suman.
      - agotadas es el *saldo* de documentos que ya gastaron los intentos → se
        queda el último valor visto, no se suma (si no, 18 corridas al día lo
        multiplicarían por 18).
      - los errores de conexión se deduplican por cliente: el mismo cliente
        inalcanzable aparece una vez, no una por tipo de documento.
    """
    por_tipo: dict[str, dict] = {}
    conexion: dict[str, dict] = {}
    arranque: list[dict] = []
    atascados: dict[tuple, dict] = {}

    for entrada in entradas:
        etiq = entrada["etiqueta"]
        datos = por_tipo.setdefault(etiq, {
            "exitosas": 0, "fallidas": 0, "ya_emitidas": 0,
            "clientes": {}, "fallos_detalle": [], "errores_ejecucion": [],
        })
        res = entrada.get("resumen") or {}

        datos["exitosas"]       += res.get("exitosas", 0)
        datos["fallidas"]       += res.get("fallidas", 0)
        datos["ya_emitidas"]    += res.get("ya_emitidas", 0)
        datos["fallos_detalle"] += res.get("fallidas_detalle", [])

        for cx in res.get("errores_conexion", []):
            conexion.setdefault(cx.get("key_cli") or cx.get("cliente", "?"), cx)

        # Los atascados son un saldo: el mismo documento aparece en todas las
        # corridas del día. Se indexa por documento y gana el último visto.
        for ficha in res.get("atascados", []):
            atascados[(ficha.get("tipo"), ficha.get("key_cli"), ficha.get("documento"))] = ficha

        if res.get("error"):
            # El subprocess no pudo ni empezar: es lo más grave que puede pasar
            # y antes se reportaba como una jornada sin novedad.
            arranque.append({
                "etiqueta":  etiq,
                "key_cli":   entrada.get("key_cli", "todos"),
                "error":     res["error"],
                "timestamp": entrada["timestamp"],
            })
        elif not entrada["exito"]:
            datos["errores_ejecucion"].append({
                "key_cli":    entrada.get("key_cli", "todos"),
                "timestamp":  entrada["timestamp"],
                "returncode": entrada["returncode"],
                "stderr":     entrada["stderr"],
            })

        for r in res.get("resultados_por_cliente", []):
            if _ETIQUETAS_DIAN.get(r.get("tipo")) != etiq:
                continue
            acc = datos["clientes"].setdefault(r["cliente"], {
                "cliente": r["cliente"], "exitosas": 0, "fallidas": 0,
                "omitidas": 0, "ya_emitidas": 0, "agotadas": 0,
            })
            for campo in ("exitosas", "fallidas", "omitidas", "ya_emitidas"):
                acc[campo] += r.get(campo, 0)
            acc["agotadas"] = r.get("agotadas", 0)   # saldo, no delta

    return {
        "por_tipo":  por_tipo,
        "conexion":  conexion,
        "arranque":  arranque,
        "atascados": list(atascados.values()),
    }


def _contar(datos: dict) -> str:
    """Resumen de una línea con los conteos que no son cero."""
    partes = []
    for campo, etiqueta in (
        ("exitosas", "exitosa(s)"), ("fallidas", "fallida(s)"),
        ("ya_emitidas", "ya emitida(s)"), ("omitidas", "omitida(s)"),
        ("agotadas", "sin intentos restantes"),
    ):
        if datos.get(campo):
            partes.append(f"{datos[campo]} {etiqueta}")
    return ", ".join(partes)


# Cómo se presenta cada causa. El orden es el de la acción a tomar: primero lo
# que se arregla en la base de datos, al final lo que solo hay que reintentar.
_CAUSAS_DIAN = (
    ("datos",   "Faltan datos en la base de datos",
                "Completar el dato faltante y reiniciar los intentos"),
    ("dian",    "Rechazados por la DIAN",
                "Corregir el documento según la validación"),
    ("tecnica", "Errores técnicos o transitorios",
                "Reintentables: reiniciar el contador de intentos"),
)


def _agrupar_por_causa(atascados: list) -> dict[str, list]:
    grupos = {causa: [] for causa, _, _ in _CAUSAS_DIAN}
    for ficha in atascados:
        grupos.setdefault(ficha.get("causa", "tecnica"), []).append(ficha)
    return grupos


def _construir_reporte_atascados(atascados: list, fecha_label: str) -> tuple[str, list, str]:
    """Reporte de documentos que agotaron los intentos, agrupados por causa.

    Un documento sin intentos restantes no vuelve a salir solo nunca más: el
    reporte tiene que decir cuál es, por qué se quedó y qué hacer con él.
    """
    grupos = _agrupar_por_causa(atascados)
    total = len(atascados)
    estado = "Sin pendientes" if total == 0 else f"{total} documento(s) atascado(s)"

    resultados = []
    mensaje = f"Documentos DIAN sin intentos restantes — {fecha_label}\n\n"

    if not total:
        mensaje += "No hay documentos atascados. Todo lo pendiente sigue reintentándose.\n"
        return mensaje, resultados, estado

    mensaje += "Estos documentos agotaron los intentos y NO se reenviarán solos.\n\n"

    for causa, titulo, accion in _CAUSAS_DIAN:
        fichas = grupos.get(causa) or []
        if not fichas:
            continue

        mensaje += f"{titulo} ({len(fichas)})\n  Acción: {accion}\n"
        for f in sorted(fichas, key=lambda x: (x.get("cliente", ""), x.get("documento", ""))):
            mensaje += f"    {f.get('cliente', '?')} · {f.get('documento', '?')}"
            mensaje += f"  [{f.get('codigo', '')}] {f.get('razon', '')}\n"
            if f.get("mensaje"):
                mensaje += f"        {f['mensaje']}\n"

            detalle = f"{f.get('razon', '')}"
            if f.get("mensaje"):
                detalle += f"\n{f['mensaje']}"
            detalle += f"\n{f.get('intentos', '?')} intento(s) · último: {f.get('ultimo_intento', 's/f')}"
            resultados.append({
                "proceso": f"{f.get('documento', '?')}  —  {f.get('cliente', '')}",
                "estado":  "ERROR",
                "detalle": f"[{titulo}] [{f.get('codigo', '')}] {detalle}",
            })
        mensaje += "\n"

    mensaje += f"Total: {total} documento(s).\n"
    return mensaje, resultados, estado


def _construir_reporte(consolidado: dict, fecha_label: str) -> tuple[str, list, str]:
    """Devuelve (texto plano, filas para el HTML, estado global)."""
    por_tipo = consolidado["por_tipo"]
    conexion = consolidado["conexion"]
    arranque = consolidado["arranque"]

    total_exitosas = sum(v["exitosas"] for v in por_tipo.values())
    total_fallidas = sum(v["fallidas"] for v in por_tipo.values())
    total_agotadas = sum(
        c["agotadas"] for v in por_tipo.values() for c in v["clientes"].values()
    )
    hay_errores = bool(
        total_fallidas or total_agotadas or conexion or arranque
        or consolidado.get("atascados")
        or any(v["errores_ejecucion"] for v in por_tipo.values())
    )
    estado_global = "Con errores" if hay_errores else "Sin errores"

    # ── Filas para la plantilla HTML ─────────────────────────────────────────
    resultados = []
    for err in arranque:
        resultados.append({
            "proceso": f"No se pudo ejecutar — {err['etiqueta']} ({err['key_cli']})",
            "estado":  "ERROR",
            "detalle": f"{err['error']}\n({err['timestamp']})",
        })
    for cx in conexion.values():
        resultados.append({
            "proceso": f"Sin acceso  —  {cx.get('cliente', cx.get('key_cli', '?'))}",
            "estado":  "ERROR",
            "detalle": f"No se pudo conectar a la base de datos del cliente: {cx.get('error', '')}",
        })
    for etiq, datos in por_tipo.items():
        for cli in datos["clientes"].values():
            resultados.append({
                "proceso": f"{etiq.capitalize()} — {cli['cliente']}",
                "estado":  "ERROR" if (cli["fallidas"] or cli["agotadas"]) else "OK",
                "detalle": _contar(cli) or "Sin documentos procesados",
            })
        for f in datos["fallos_detalle"]:
            resultados.append({
                "proceso": f"{f.get('factura', '?')}  —  {f.get('cliente', '')}",
                "estado":  "ERROR",
                "detalle": f"[{f.get('codigo', '')}]\n{_formatear_fallo(f)}",
            })
        for err in datos["errores_ejecucion"]:
            resultados.append({
                "proceso": f"Error de ejecución — {etiq} ({err['key_cli']})",
                "estado":  "ERROR",
                "detalle": f"returncode={err['returncode']}  ({err['timestamp']})\n{err['stderr']}",
            })

    # ── Cuerpo en texto plano ────────────────────────────────────────────────
    mensaje = f"Reporte consolidado DIAN — {fecha_label}\n\n"

    if arranque:
        mensaje += "EJECUCIONES QUE NO ARRANCARON\n"
        for err in arranque:
            mensaje += f"  {err['etiqueta']} ({err['key_cli']}): {err['error']}\n"
        mensaje += "\n"

    if conexion:
        mensaje += "CLIENTES SIN ACCESO\n"
        for cx in conexion.values():
            mensaje += f"  {cx.get('cliente', '?')}: {cx.get('error', '')}\n"
        mensaje += "\n"

    for etiq, datos in por_tipo.items():
        mensaje += (
            f"{etiq.capitalize()}\n"
            f"  Exitosos   : {datos['exitosas']}\n"
            f"  Fallidos   : {datos['fallidas']}\n"
        )
        if datos["ya_emitidas"]:
            mensaje += f"  Ya emitidos: {datos['ya_emitidas']}\n"
        if datos["clientes"]:
            mensaje += "  Por cliente:\n"
            for cli in datos["clientes"].values():
                mensaje += f"    {cli['cliente']}: {_contar(cli) or 'sin documentos'}\n"
        if datos["fallos_detalle"]:
            mensaje += f"\n  Detalle de {etiq} fallidos:\n"
            for f in datos["fallos_detalle"]:
                mensaje += f"\n    {f.get('factura', '?')}  ({f.get('cliente', '')})  [{f.get('codigo', '')}]\n"
                for linea in _formatear_fallo(f).splitlines():
                    mensaje += f"      {linea}\n"
        for err in datos["errores_ejecucion"]:
            mensaje += f"\n  Error de ejecución ({err['timestamp']}): code={err['returncode']}\n"
            if err["stderr"]:
                mensaje += f"  {err['stderr']}\n"
        mensaje += "\n"

    mensaje += f"Total exitosos : {total_exitosas}\n"
    mensaje += f"Total fallidos : {total_fallidas}\n"
    if conexion:
        mensaje += f"Clientes sin acceso   : {len(conexion)}\n"

    # Los atascados no se detallan aquí: van completos en el reporte dedicado
    # (tasks.reenvio_dian.reportar_documentos_atascados). Aquí solo el titular,
    # para que el reporte diario no se convierta en una lista de 40 filas.
    atascados = consolidado.get("atascados") or []
    if atascados:
        grupos = _agrupar_por_causa(atascados)
        desglose = ", ".join(
            f"{len(grupos[causa])} {titulo.lower()}"
            for causa, titulo, _ in _CAUSAS_DIAN if grupos.get(causa)
        )
        mensaje += f"\nSin intentos restantes: {len(atascados)} documento(s) — {desglose}.\n"
        mensaje += "No se reintentarán solos; ver el reporte de documentos atascados.\n"
        resultados.append({
            "proceso": "Documentos sin intentos restantes",
            "estado":  "ERROR",
            "detalle": f"{len(atascados)} documento(s): {desglose}",
        })
    elif total_agotadas:
        # Saldo sin ficha: viene de una corrida anterior al detalle por documento.
        mensaje += f"\nSin intentos restantes: {total_agotadas} documento(s).\n"

    return mensaje, resultados, estado_global


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
    consolidado del día.

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

    resultados = []
    for cli in clientes:
        for tipo in tipos:
            etiqueta  = _ETIQUETAS_DIAN[tipo]
            cli_label = cli or "todos"
            logger.info(f"Procesando [{etiqueta}] cliente={cli_label}")

            try:
                resultado = _ejecutar_reenvio(tipo, cli, env_config)
            except Exception as exc:
                logger.error(
                    f"Error inesperado al ejecutar reenvío DIAN "
                    f"[{etiqueta}] cliente={cli_label}: {exc}"
                )
                # Un timeout o un intérprete inexistente también son resultado:
                # sin acumularlos, la ejecución desaparece del reporte diario.
                _acumular_resultado_dian(
                    subprocess.CompletedProcess(args=[], returncode=-1, stdout="", stderr=str(exc)[:1000]),
                    False, tipo_doc=tipo, etiqueta=etiqueta, key_cli=cli_label,
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

            _acumular_resultado_dian(resultado, exito, tipo_doc=tipo, etiqueta=etiqueta,
                                     key_cli=cli_label)
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
    Programar una vez al día.

    kwargs aceptados:
        destinatarios — lista de correos destino; None = usa MAIL_TO del entorno.
    """
    from tasks.correo import enviar_correo

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
    consolidado = _consolidar(entradas)
    fecha_label = ahora().strftime("%d/%m/%Y")
    mensaje, resultados_email, estado_global = _construir_reporte(consolidado, fecha_label)

    enviar_correo.delay(
        asunto=f"Reporte DIAN — {fecha_label} — {estado_global}",
        mensaje=mensaje,
        datos_reporte={"resultados": resultados_email},
        destinatarios=destinatarios,
    )
    logger.info(
        f"Reporte DIAN diario enviado ({len(entradas)} ejecución(es) consolidadas, "
        f"tipos: {list(consolidado['por_tipo'])})."
    )

    try:
        _get_redis().delete(clave)
    except Exception:
        pass


@celery_app.task(name="tasks.reenvio_dian.reportar_documentos_atascados")
def reportar_documentos_atascados(destinatarios: list = None, solo_si_hay: bool = True, **_):
    """
    Envía el listado de documentos que agotaron los MAX_INTENTOS y ya no se
    reenviarán solos, agrupados por causa: falta de datos, rechazo de la DIAN o
    error técnico.

    Se alimenta de las mismas ejecuciones acumuladas en Redis que el reporte
    diario, así que refleja el estado real en la DB de cada cliente (no solo el
    log de errores): un documento que alguien emitió a mano ya no aparece.

    kwargs aceptados:
        destinatarios — lista de correos destino; None = usa MAIL_TO del entorno.
        solo_si_hay   — True (defecto) no envía nada si no hay atascados;
                        False envía igual para confirmar que el proceso corrió.

    Programar después de la última corrida de reenvío del día.
    """
    from tasks.correo import enviar_correo

    clave = f"{_REDIS_KEY_PREFIX}:{_fecha_hoy()}"
    try:
        raw_list = _get_redis().lrange(clave, 0, -1)
    except Exception as exc:
        logger.error(f"No se pudo leer el reporte DIAN de Redis: {exc}")
        return

    if not raw_list:
        logger.info("Documentos atascados: sin ejecuciones registradas hoy.")
        return

    atascados = _consolidar([json.loads(e) for e in raw_list])["atascados"]

    if not atascados and solo_si_hay:
        logger.info("Documentos atascados: ninguno. No se envía correo.")
        return

    fecha_label = ahora().strftime("%d/%m/%Y")
    mensaje, resultados, estado = _construir_reporte_atascados(atascados, fecha_label)

    enviar_correo.delay(
        asunto=f"Documentos DIAN sin intentos restantes — {fecha_label} — {estado}",
        mensaje=mensaje,
        datos_reporte={"resultados": resultados},
        destinatarios=destinatarios,
    )
    logger.info(f"Reporte de documentos atascados enviado ({len(atascados)} documento(s)).")
    return mensaje


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
