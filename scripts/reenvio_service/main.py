"""
Servicio externo de reenvío automático de facturas a la DIAN.

Uso:
    # Procesar todos los clientes con transmitir=true
    python -m reenvio_service.main

    # Procesar solo un cliente específico
    python -m reenvio_service.main --key-cli <KEY_CLI>
"""
import asyncio
import argparse
import json
import os
from datetime import datetime

from reenvio_service.config import (
    KEY_CLI_FILTER,
    API_PYTHON_URL,
    API_PYTHON_USERNAME,
    API_PYTHON_PASSWORD,
)
from reenvio_service.db_clientes import create_main_pool, get_all_clientes, get_cliente_by_key
from reenvio_service.error_log import ensure_error_table
from reenvio_service.reenvio import reenviar_cliente, obtener_token

SEP = '=' * 70


async def main(key_cli_filter: str = None) -> None:
    inicio = datetime.now()
    print(f"\n{SEP}")
    print(f"  REENVIO AUTOMATICO DIAN  |  {inicio.strftime('%Y-%m-%d %H:%M:%S')}")
    print(SEP)

    # 1. Validar configuración de la API
    if not API_PYTHON_URL:
        print("[ERROR] API_PYTHON_URL no está configurada. Verifica las credenciales en scheduler_credentials.")
        return
    if not API_PYTHON_USERNAME or not API_PYTHON_PASSWORD:
        print("[ERROR] API_PYTHON_USERNAME o API_PYTHON_PASSWORD no están configuradas.")
        return
    print(f"[AUTH] URL={API_PYTHON_URL} | user={API_PYTHON_USERNAME}")

    # 2. Autenticar contra la API y obtener token
    print("[AUTH] Obteniendo token...")
    token = await obtener_token(API_PYTHON_USERNAME, API_PYTHON_PASSWORD)
    print("[AUTH] Token obtenido\n")

    # 2. Obtener clientes y preparar tabla de errores en DB central
    main_pool = await create_main_pool()
    try:
        async with main_pool.acquire() as conn:
            await ensure_error_table(conn)

        filtro = key_cli_filter or KEY_CLI_FILTER

        if filtro:
            cliente = await get_cliente_by_key(main_pool, filtro)
            if not cliente:
                print(f"[ERROR] No se encontró cliente con key_cli='{filtro}'")
                return
            clientes = [cliente]
        else:
            clientes = await get_all_clientes(main_pool)

        if not clientes:
            print("[INFO] No hay clientes con transmitir=true. Nada que procesar.")
            return

        print(f"[INFO] Clientes a procesar: {len(clientes)}\n")

        # 3. Procesar cada cliente
        resultados = []
        for i, cliente in enumerate(clientes, 1):
            modo = 'PROD' if cliente['produccion'] else 'TEST'
            print(f"[{i}/{len(clientes)}] {cliente['nombre_cliente']} ({cliente['key_cli']}) | {modo}")

            resultado = await reenviar_cliente(cliente, token, main_pool)
            resultados.append(resultado)

            if resultado.get('connection_error'):
                print(f"  [X] Error de conexión: {resultado['connection_error']}")
            elif resultado['total'] == 0:
                print(f"  [=] Sin facturas pendientes")
            else:
                print(
                    f"  [R] Total={resultado['total']} | "
                    f"OK={resultado['exitosas']} | "
                    f"Fail={resultado['fallidas']} | "
                    f"Omitidas={resultado['omitidas']}"
                )

    finally:
        await main_pool.close()

    # 4. Resumen final
    duracion       = (datetime.now() - inicio).total_seconds()
    total_facturas = sum(r['total']    for r in resultados)
    total_ok       = sum(r['exitosas'] for r in resultados)
    total_fail     = sum(r['fallidas'] for r in resultados)
    con_errores_cx = sum(1 for r in resultados if r.get('connection_error'))

    print(f"\n{SEP}")
    print(f"  RESUMEN FINAL")
    print(SEP)
    print(f"  Clientes procesados  : {len(resultados)}")
    if con_errores_cx:
        print(f"  Errores de conexión  : {con_errores_cx}")
    print(f"  Facturas procesadas  : {total_facturas}")
    print(f"  Exitosas             : {total_ok}")
    print(f"  Fallidas             : {total_fail}")
    print(f"  Duración             : {duracion:.1f}s")

    print(SEP + "\n")

    # Línea JSON estructurada para la tarea Celery (correo de notificación)
    todas_fallidas = [f for r in resultados for f in r.get('fallidas_detalle', [])]
    resumen_json = {
        "clientes":   len(resultados),
        "facturas":   total_facturas,
        "exitosas":   total_ok,
        "fallidas":   total_fail,
        "errores_cx": con_errores_cx,
        "fallidas_detalle": todas_fallidas,
    }
    print(f"RESUMEN_JSON:{json.dumps(resumen_json, ensure_ascii=False)}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Reenvío automático de facturas a la DIAN (máx. 3 intentos por factura)'
    )
    parser.add_argument(
        '--key-cli',
        type=str,
        default=None,
        help='Procesar solo este cliente (key_cli). Sin valor = todos los clientes.',
    )
    args = parser.parse_args()
    asyncio.run(main(key_cli_filter=args.key_cli))
