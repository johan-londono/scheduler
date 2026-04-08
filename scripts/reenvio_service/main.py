"""
Servicio externo de reenvío automático de documentos electrónicos a la DIAN.

Tipos de documento procesados:
  - Facturas electrónicas    (FE)
  - Documentos soporte       (DS)
  - Notas crédito            (NC)

Uso:
    # Procesar todos los clientes con transmitir=true
    python -m reenvio_service.main

    # Procesar solo un cliente específico
    python -m reenvio_service.main --key-cli <KEY_CLI>

    # Procesar solo un tipo de documento
    python -m reenvio_service.main --tipo facturas
    python -m reenvio_service.main --tipo docsoporte
    python -m reenvio_service.main --tipo nc

Cron (ejemplo: cada hora):
    0 * * * * cd /ruta/al/proyecto && python -m reenvio_service.main >> /var/log/reenvio_dian.log 2>&1
"""
import asyncio
import argparse
import sys
import traceback
from datetime import datetime

from reenvio_service.config import (
    PROVEEDOR_INTEGRACION, KEY_CLI_FILTER,
    API_PYTHON_USERNAME, API_PYTHON_PASSWORD,
)
from reenvio_service.db_clientes import create_main_pool, create_scheduler_pool, get_all_clientes, get_cliente_by_key
from reenvio_service.reenvio import reenviar_cliente, obtener_token
from reenvio_service.reenvio_docsoporte import reenviar_cliente_docsoporte
from reenvio_service.reenvio_nc import reenviar_cliente_nc

SEP = '=' * 70

TIPOS_VALIDOS = ('facturas', 'docsoporte', 'nc')


def _print_resultado(label: str, resultado: dict) -> None:
    if resultado.get('connection_error'):
        print(f"  [X] {label} - Error de conexión: {resultado['connection_error']}")
    elif resultado['total'] == 0:
        print(f"  [=] {label} - Sin documentos pendientes")
    else:
        print(
            f"  [R] {label} - Total={resultado['total']} | "
            f"OK={resultado['exitosas']} | "
            f"Fail={resultado['fallidas']} | "
            f"Omitidas={resultado['omitidas']}"
        )


async def main(key_cli_filter: str = None, tipo: str = None) -> None:
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
    except Exception:
        print("[AUTH] Error al obtener token:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return
    print("[AUTH] Token obtenido\n")

    # 2. Obtener clientes y abrir pool del scheduler (tablas de errores DIAN)
    try:
        main_pool = await create_main_pool()
    except Exception:
        print("[DB] Error al conectar a la DB principal (MAIN_DB_*):", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return

    try:
        scheduler_pool = await create_scheduler_pool()
    except Exception:
        await main_pool.close()
        print("[DB] Error al conectar a la DB del scheduler (SCHEDULER_DB_*):", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return

    try:
        filtro = key_cli_filter or KEY_CLI_FILTER

        if filtro:
            cliente = await get_cliente_by_key(main_pool, filtro)
            if not cliente:
                print(f"[ERROR] No se encontró cliente con key_cli='{filtro}'")
                return
            clientes = [cliente]
        else:
            clientes = await get_all_clientes(main_pool)
    finally:
        await main_pool.close()

    if not clientes:
        print("[INFO] No hay clientes con transmitir=true. Nada que procesar.")
        await scheduler_pool.close()
        return

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
        cx_err = sum(1 for r in resultados if r.get('connection_error'))
        print(f"  {label:<22} Total={total} | OK={ok} | Fail={fail}" +
              (f" | CxErr={cx_err}" if cx_err else ""))

    _totales(res_facturas,   "Facturas")
    _totales(res_docsoporte, "Doc. Soporte")
    _totales(res_nc,         "Notas Crédito")

    print(f"  Duración             : {duracion:.1f}s")
    print(SEP + "\n")

    # Línea JSON estructurada para la tarea Celery (correo de notificación)
    import json as _json
    todas_listas  = res_facturas + res_nc + res_docsoporte
    todas_fallidas = [f for r in todas_listas for f in r.get('fallidas_detalle', [])]
    errores_conexion = [
        {"key_cli": r['key_cli'], "cliente": r['nombre'], "error": r['connection_error']}
        for r in todas_listas if r.get('connection_error')
    ]
    # Desglose por cliente y tipo (solo clientes con documentos encontrados)
    resultados_por_cliente = []
    for tipo_label, tipo_resultados in [
        ("facturas",           res_facturas),
        ("notas_credito",      res_nc),
        ("documentos_soporte", res_docsoporte),
    ]:
        for r in tipo_resultados:
            if r['total'] > 0:
                resultados_por_cliente.append({
                    "key_cli":  r['key_cli'],
                    "cliente":  r['nombre'],
                    "tipo":     tipo_label,
                    "exitosas": r['exitosas'],
                    "fallidas": r['fallidas'],
                    "omitidas": r.get('omitidas', 0),
                })
    resumen_json = {
        "clientes":                len(clientes),
        "facturas":                sum(r['total']    for r in todas_listas),
        "exitosas":                sum(r['exitosas'] for r in todas_listas),
        "fallidas":                sum(r['fallidas'] for r in todas_listas),
        "errores_cx":              len({r['key_cli'] for r in todas_listas if r.get('connection_error')}),
        "fallidas_detalle":        todas_fallidas,
        "errores_conexion":        errores_conexion,
        "nombres_clientes":        [c['nombre_cliente'] for c in clientes],
        "resultados_por_cliente":  resultados_por_cliente,
    }
    print(f"RESUMEN_JSON:{_json.dumps(resumen_json, ensure_ascii=False)}")


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
    asyncio.run(main(key_cli_filter=args.key_cli, tipo=args.tipo))
