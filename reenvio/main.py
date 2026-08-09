"""
Servicio externo de reenvío automático de documentos electrónicos a la DIAN.

Tipos de documento procesados:
  - Facturas electrónicas    (FE)
  - Documentos soporte       (DS)
  - Notas crédito            (NC)

Uso:
    # Procesar todos los clientes con transmitir=true
    python -m reenvio.main

    # Procesar solo un cliente específico
    python -m reenvio.main --key-cli <KEY_CLI>

    # Procesar solo un tipo de documento
    python -m reenvio.main --tipo facturas
    python -m reenvio.main --tipo docsoporte
    python -m reenvio.main --tipo nc

Cron (ejemplo: cada hora):
    0 * * * * cd /ruta/al/proyecto && python -m reenvio.main >> /var/log/reenvio_dian.log 2>&1
"""
import asyncio
import argparse
import json
import sys
import traceback
from datetime import datetime

from reenvio.config import (
    PROVEEDOR_INTEGRACION, KEY_CLI_FILTER,
    API_PYTHON_USERNAME, API_PYTHON_PASSWORD,
)
from reenvio.clientes import create_main_pool, create_scheduler_pool, get_all_clientes, get_cliente_by_key
from reenvio.facturas import reenviar_cliente, obtener_token
from reenvio.docsoporte import reenviar_cliente_docsoporte
from reenvio.notas_credito import reenviar_cliente_nc

SEP = '=' * 70

TIPOS_VALIDOS = ('facturas', 'docsoporte', 'nc')


def _print_resultado(label: str, resultado: dict) -> None:
    if resultado.get('connection_error'):
        print(f"  [X] {label} - Error de conexión: {resultado['connection_error']}")
    elif resultado['total'] == 0 and not resultado.get('agotadas'):
        print(f"  [=] {label} - Sin documentos pendientes")
    else:
        print(
            f"  [R] {label} - Total={resultado['total']} | "
            f"OK={resultado['exitosas']} | "
            f"Fail={resultado['fallidas']} | "
            f"YaEmitidas={resultado.get('ya_emitidas', 0)} | "
            f"Agotadas={resultado.get('agotadas', 0)} | "
            f"Omitidas={resultado['omitidas']}"
        )


def _emitir(resumen: dict) -> None:
    """Imprime la línea que consume la tarea Celery.

    Se emite SIEMPRE, también en los fallos de arranque: sin ella la tarea no
    distingue "no había nada que hacer" de "no se pudo ni empezar".
    """
    print(f"RESUMEN_JSON:{json.dumps(resumen, ensure_ascii=False)}")


def _fallo(mensaje: str) -> int:
    """Emite un resumen vacío con la causa y devuelve el código de salida 1."""
    _emitir({
        "clientes": 0, "facturas": 0, "exitosas": 0, "fallidas": 0,
        "ya_emitidas": 0, "agotadas": 0, "errores_cx": 0,
        "fallidas_detalle": [], "atascados": [], "errores_conexion": [],
        "nombres_clientes": [], "resultados_por_cliente": [],
        "error": mensaje,
    })
    return 1


async def main(key_cli_filter: str = None, tipo: str = None) -> int:
    inicio = datetime.now()
    print(f"\n{SEP}")
    print(f"  REENVIO AUTOMATICO DIAN  |  {inicio.strftime('%Y-%m-%d %H:%M:%S')}")
    if tipo:
        print(f"  Tipo de documento        : {tipo.upper()}")
    print(SEP)

    # 1. Autenticar contra la API y obtener token
    print(f"[AUTH] proveedor={PROVEEDOR_INTEGRACION} | Obteniendo token...")
    try:
        token = await obtener_token(API_PYTHON_USERNAME, API_PYTHON_PASSWORD)
    except Exception as exc:
        print("[AUTH] Error al obtener token:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return _fallo(f"No se pudo autenticar contra la API DIAN: {exc}")
    print("[AUTH] Token obtenido\n")

    # 2. Obtener clientes y abrir pool del scheduler (tablas de errores DIAN)
    try:
        main_pool = await create_main_pool()
    except Exception as exc:
        print("[DB] Error al conectar a la DB principal (MAIN_DB_*):", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return _fallo(f"Sin conexión a la DB maestra eSuite (MAIN_DB_*): {exc}")

    try:
        scheduler_pool = await create_scheduler_pool()
    except Exception as exc:
        await main_pool.close()
        print("[DB] Error al conectar a la DB del scheduler (SCHEDULER_DB_*):", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return _fallo(f"Sin conexión a la DB del scheduler (SCHEDULER_DB_*): {exc}")

    filtro = key_cli_filter or KEY_CLI_FILTER
    try:
        if filtro:
            cliente  = await get_cliente_by_key(main_pool, filtro)
            clientes = [cliente] if cliente else []
        else:
            clientes = await get_all_clientes(main_pool)
    finally:
        await main_pool.close()

    if not clientes:
        await scheduler_pool.close()
        if filtro:
            print(f"[ERROR] No se encontró cliente con key_cli='{filtro}'", file=sys.stderr)
            return _fallo(f"No existe el cliente key_cli={filtro}")
        print("[ERROR] No hay clientes con transmitir=true. Nada que procesar.", file=sys.stderr)
        return _fallo("No hay clientes con transmitir=true en clientes_conexiones_db")

    print(f"[INFO] Clientes a procesar: {len(clientes)}\n")

    procesar_facturas   = tipo is None or tipo == 'facturas'
    procesar_docsoporte = tipo is None or tipo == 'docsoporte'
    procesar_nc         = tipo is None or tipo == 'nc'

    # 3. Procesar cada cliente
    res_facturas   = []
    res_docsoporte = []
    res_nc         = []

    try:
        for i, cliente in enumerate(clientes, 1):
            modo    = 'PROD' if cliente['produccion'] else 'TEST'
            key_cli = cliente['key_cli']
            nombre  = cliente['nombre_cliente']
            print(f"[{i}/{len(clientes)}] {nombre} ({key_cli}) | {modo}")

            if procesar_facturas:
                try:
                    r = await reenviar_cliente(cliente, token, scheduler_pool)
                except Exception:
                    print(f"  [FATAL] Facturas — {nombre} ({key_cli}):", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
                    r = {'key_cli': key_cli, 'nombre': nombre, 'total': 0,
                         'exitosas': 0, 'fallidas': 0, 'omitidas': 0,
                         'connection_error': 'excepcion_no_controlada'}
                res_facturas.append(r)
                _print_resultado("Facturas", r)

            if procesar_docsoporte:
                try:
                    r = await reenviar_cliente_docsoporte(cliente, token, scheduler_pool)
                except Exception:
                    print(f"  [FATAL] DocSoporte — {nombre} ({key_cli}):", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
                    r = {'key_cli': key_cli, 'nombre': nombre, 'total': 0,
                         'exitosas': 0, 'fallidas': 0, 'omitidas': 0,
                         'connection_error': 'excepcion_no_controlada'}
                res_docsoporte.append(r)
                _print_resultado("DocSoporte", r)

            if procesar_nc:
                try:
                    r = await reenviar_cliente_nc(cliente, token, scheduler_pool)
                except Exception:
                    print(f"  [FATAL] NC — {nombre} ({key_cli}):", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
                    r = {'key_cli': key_cli, 'nombre': nombre, 'total': 0,
                         'exitosas': 0, 'fallidas': 0, 'omitidas': 0,
                         'connection_error': 'excepcion_no_controlada'}
                res_nc.append(r)
                _print_resultado("NC", r)
    finally:
        await scheduler_pool.close()

    # 4. Resumen final
    duracion = (datetime.now() - inicio).total_seconds()

    print(f"\n{SEP}")
    print(f"  RESUMEN FINAL")
    print(SEP)
    print(f"  Clientes procesados  : {len(clientes)}")

    def _totales(resultados: list[dict], label: str) -> None:
        if not resultados:
            return
        total = sum(r['total']    for r in resultados)
        ok    = sum(r['exitosas'] for r in resultados)
        fail  = sum(r['fallidas'] for r in resultados)
        agot  = sum(r.get('agotadas', 0) for r in resultados)
        cx_err = sum(1 for r in resultados if r.get('connection_error'))
        print(f"  {label:<22} Total={total} | OK={ok} | Fail={fail}" +
              (f" | Agotadas={agot}" if agot else "") +
              (f" | CxErr={cx_err}" if cx_err else ""))

    _totales(res_facturas,   "Facturas")
    _totales(res_docsoporte, "Doc. Soporte")
    _totales(res_nc,         "Notas Crédito")

    print(f"  Duración             : {duracion:.1f}s")
    print(SEP + "\n")

    # Línea JSON estructurada para la tarea Celery (correo de notificación)
    todas_listas  = res_facturas + res_nc + res_docsoporte
    todas_fallidas = [f for r in todas_listas for f in r.get('fallidas_detalle', [])]

    # Documentos que ya no se reintentarán solos, con su última causa conocida
    atascados = []
    for tipo_label, tipo_resultados in (
        ("facturas", res_facturas), ("notas_credito", res_nc), ("documentos_soporte", res_docsoporte),
    ):
        for r in tipo_resultados:
            for ficha in r.get('agotadas_detalle', []):
                atascados.append({**ficha, "tipo": tipo_label})

    # Un cliente inalcanzable falla en los tres tipos: una entrada por cliente,
    # no una por (cliente, tipo), o el correo repite la misma fila tres veces.
    errores_conexion = list({
        r['key_cli']: {"key_cli": r['key_cli'], "cliente": r['nombre'], "error": r['connection_error']}
        for r in todas_listas if r.get('connection_error')
    }.values())

    # Desglose por cliente y tipo (con documentos pendientes o atascados)
    resultados_por_cliente = []
    for tipo_label, tipo_resultados in [
        ("facturas",           res_facturas),
        ("notas_credito",      res_nc),
        ("documentos_soporte", res_docsoporte),
    ]:
        for r in tipo_resultados:
            if r['total'] > 0 or r.get('agotadas'):
                resultados_por_cliente.append({
                    "key_cli":     r['key_cli'],
                    "cliente":     r['nombre'],
                    "tipo":        tipo_label,
                    "exitosas":    r['exitosas'],
                    "fallidas":    r['fallidas'],
                    "omitidas":    r.get('omitidas', 0),
                    "ya_emitidas": r.get('ya_emitidas', 0),
                    "agotadas":    r.get('agotadas', 0),
                })

    _emitir({
        "clientes":                len(clientes),
        "facturas":                sum(r['total']    for r in todas_listas),
        "exitosas":                sum(r['exitosas'] for r in todas_listas),
        "fallidas":                sum(r['fallidas'] for r in todas_listas),
        "ya_emitidas":             sum(r.get('ya_emitidas', 0) for r in todas_listas),
        "agotadas":                sum(r.get('agotadas', 0)    for r in todas_listas),
        "errores_cx":              len(errores_conexion),
        "fallidas_detalle":        todas_fallidas,
        "atascados":               atascados,
        "errores_conexion":        errores_conexion,
        "nombres_clientes":        [c['nombre_cliente'] for c in clientes],
        "resultados_por_cliente":  resultados_por_cliente,
    })

    # El código de salida responde "¿pude ejecutar?"; el qué pasó con cada
    # cliente va en RESUMEN_JSON, que la tarea acumula pase lo que pase.
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Reenvío automático de documentos electrónicos a la DIAN'
    )
    parser.add_argument(
        '--key-cli',
        type=str,
        default=None,
        help='Procesar solo este cliente (key_cli). Sin valor = todos los clientes.',
    )
    parser.add_argument(
        '--tipo',
        type=str,
        choices=TIPOS_VALIDOS,
        default=None,
        help='Tipo de documento a procesar: facturas, docsoporte, nc. Sin valor = todos.',
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(key_cli_filter=args.key_cli, tipo=args.tipo)))
