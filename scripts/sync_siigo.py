#!/usr/bin/env python3
"""
sync_siigo.py — Script de sincronización de módulos Siigo

Uso básico:
    python sync_siigo.py --url http://localhost:8000 \
        --username admin@empresa.com --password mipass --key 123 \
        --siigo-username usuario@siigo.com --siigo-access-key ACCESS_KEY \
        --modules invoices customers \
        --date-start 2025-01-01 --date-end 2025-01-31

Todos los módulos:
    python sync_siigo.py ... --all

Ver módulos disponibles:
    python sync_siigo.py --list-modules
"""

import argparse
import sys
import json
import math
from datetime import datetime

try:
    import requests
except ImportError:
    print("ERROR: requiere 'requests'. Instalar con: pip install requests")
    sys.exit(1)


# ─────────────────────────────────────────────
#  Definición de módulos
# ─────────────────────────────────────────────

# date_fields: qué campos de fecha acepta el módulo
#   "created"   → created_start / created_end
#   "date"      → date_start / date_end  (además de created)
#   "due"       → due_date_start / due_date_end
#   None        → no acepta fechas
# has_body: si el endpoint espera form-body con los filtros
MODULES = {
    "customers": {
        "endpoint": "/api/v1/siigo/generate/customers",
        "date_fields": "created",
        "has_body": True,
        "description": "Clientes",
    },
    "products": {
        "endpoint": "/api/v1/siigo/generate/products",
        "date_fields": "created",
        "has_body": True,
        "description": "Productos",
    },
    "invoices": {
        "endpoint": "/api/v1/siigo/generate/invoices",
        "date_fields": "date+created",
        "has_body": True,
        "description": "Facturas de venta",
    },
    "credit-notes": {
        "endpoint": "/api/v1/siigo/generate/credit-notes",
        "date_fields": "created",
        "has_body": True,
        "description": "Notas crédito",
    },
    "purchases": {
        "endpoint": "/api/v1/siigo/generate/purchases",
        "date_fields": None,
        "has_body": False,
        "description": "Compras",
    },
    "vouchers": {
        "endpoint": "/api/v1/siigo/generate/vouchers",
        "date_fields": "created",
        "has_body": True,
        "description": "Comprobantes (vouchers)",
    },
    "journals": {
        "endpoint": "/api/v1/siigo/generate/journals",
        "date_fields": None,
        "has_body": False,
        "description": "Diarios contables",
    },
    "accounts-payable": {
        "endpoint": "/api/v1/siigo/generate/accounts-payable",
        "date_fields": "due",
        "has_body": True,
        "description": "Cuentas por pagar",
    },
    "account-groups": {
        "endpoint": "/api/v1/siigo/generate/account-groups",
        "date_fields": None,
        "has_body": True,
        "description": "Grupos de cuentas",
    },
    "cost-centers": {
        "endpoint": "/api/v1/siigo/generate/cost-centers",
        "date_fields": None,
        "has_body": True,
        "description": "Centros de costo",
    },
    "warehouses": {
        "endpoint": "/api/v1/siigo/generate/warehouses",
        "date_fields": None,
        "has_body": True,
        "description": "Bodegas",
    },
    "users": {
        "endpoint": "/api/v1/siigo/generate/users",
        "date_fields": None,
        "has_body": False,
        "description": "Usuarios Siigo",
    },
    "document-types": {
        "endpoint": "/api/v1/siigo/generate/document-types",
        "date_fields": None,
        "has_body": True,
        "description": "Tipos de documento (requiere --doc-type)",
    },
    "balance-report": {
        "endpoint": "/api/v1/siigo/generate/balance-report",
        "date_fields": "balance",
        "has_body": True,
        "description": "Reporte de balance general (requiere --year y --month o --month-start/--month-end)",
    },
    "balance-report-by-thirdparty": {
        "endpoint": "/api/v1/siigo/generate/balance-report-by-thirdparty",
        "date_fields": "balance",
        "has_body": True,
        "description": "Reporte de balance por tercero (requiere --year y --month o --month-start/--month-end)",
    },
}

ALL_MODULES = list(MODULES.keys())


# ─────────────────────────────────────────────
#  Helpers de output
# ─────────────────────────────────────────────

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "  ", "OK": "✓ ", "ERR": "✗ ", "WARN": "! ", "HEAD": ""}
    print(f"[{ts}] {prefix.get(level, '')}{msg}")

def section(title):
    print(f"\n{'─' * 55}")
    print(f"  {title}")
    print(f"{'─' * 55}")


# ─────────────────────────────────────────────
#  Autenticación
# ─────────────────────────────────────────────

def login(base_url, username, password, key):
    url = f"{base_url}/api/token/"
    payload = {"username": username, "password": password, "key": key}
    log(f"Autenticando en ESuite (key={key})...")
    r = requests.post(url, data=payload, timeout=30)
    if r.status_code != 200:
        log(f"Login fallido ({r.status_code}): {r.text}", "ERR")
        sys.exit(1)
    token = r.json().get("access_token")
    if not token:
        log("Respuesta de login no contiene access_token", "ERR")
        sys.exit(1)
    log("Login exitoso", "OK")
    return token


def generate_siigo_token(base_url, jwt, siigo_username, siigo_access_key):
    url = f"{base_url}/api/v1/siigo/generate/token"
    headers = {"Authorization": f"Bearer {jwt}"}
    payload = {"username": siigo_username, "access_key": siigo_access_key}
    log("Generando token Siigo...")
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    body = r.json()
    if r.status_code != 200:
        log(f"Error generando token Siigo: {body}", "ERR")
        sys.exit(1)
    log(f"{body.get('message', 'Token generado')}", "OK")


# ─────────────────────────────────────────────
#  Construcción del form-body según módulo
# ─────────────────────────────────────────────

def build_body(module_cfg, args):
    date_type = module_cfg["date_fields"]
    body = {}

    if date_type == "created" or date_type == "date+created":
        if args.date_start:
            body["created_start"] = args.date_start
        if args.date_end:
            body["created_end"] = args.date_end

    if date_type == "date+created":
        if args.date_start:
            body["date_start"] = args.date_start
        if args.date_end:
            body["date_end"] = args.date_end

    if date_type == "due":
        if args.date_start:
            body["due_date_start"] = args.date_start
        if args.date_end:
            body["due_date_end"] = args.date_end

    if date_type == "balance":
        if not args.year:
            log("balance-report requiere --year", "ERR")
            return None
        body["year"] = args.year
        if args.account_start:
            body["account_start"] = args.account_start
        if args.account_end:
            body["account_end"] = args.account_end
        if args.month:
            body["month"] = args.month
        else:
            if not (args.month_start and args.month_end):
                log("balance-report requiere --month o el par --month-start y --month-end", "ERR")
                return None
            body["month_start"] = args.month_start
            body["month_end"] = args.month_end
        if args.includes_tax_difference is not None:
            body["includes_tax_difference"] = str(args.includes_tax_difference).lower()

    if module_cfg["endpoint"].endswith("document-types"):
        if not args.doc_type:
            log("document-types requiere --doc-type", "ERR")
            return None
        body["type"] = args.doc_type

    return body


# ─────────────────────────────────────────────
#  Llamada a un módulo (una página)
# ─────────────────────────────────────────────

def call_module(base_url, jwt, module_name, module_cfg, args, page, clear_first_page):
    url = f"{base_url}{module_cfg['endpoint']}"
    headers = {"Authorization": f"Bearer {jwt}"}

    params = {
        "page_size": args.page_size,
        "page": page,
        "clear": str(clear_first_page).lower(),
        "clear_tables": "false",
    }

    body = {}
    if module_cfg["has_body"]:
        body = build_body(module_cfg, args)
        if body is None:
            return None

    r = requests.post(url, headers=headers, params=params, data=body, timeout=120)

    try:
        data = r.json()
    except Exception:
        log(f"Respuesta no es JSON: {r.text[:200]}", "ERR")
        return None

    if r.status_code != 200:
        detail = data.get("detail", data)
        log(f"Error {r.status_code}: {json.dumps(detail, ensure_ascii=False)[:300]}", "ERR")
        return None

    return data


# ─────────────────────────────────────────────
#  Sincronización de un módulo completo
# ─────────────────────────────────────────────

def sync_module(base_url, jwt, module_name, args):
    module_cfg = MODULES[module_name]
    section(f"{module_cfg['description'].upper()}  [{module_name}]")

    # Primera página — con clear=True para limpiar datos del rango
    log(f"Página 1 / ? (page_size={args.page_size})...")
    result = call_module(base_url, jwt, module_name, module_cfg, args, page=1, clear_first_page=True)

    if result is None:
        log(f"Módulo {module_name} omitido por error.", "WARN")
        return

    detail = result.get("detail", {})
    total_results = detail.get("total_results", 0)
    total_pages = detail.get("pages", 1) or 1
    message = result.get("message", "")

    log(f"{message}  (total={total_results}, páginas={total_pages})", "OK")

    if isinstance(detail.get("saved"), list):
        for s in detail["saved"]:
            log(f"  tabla '{s['table']}': {s['total']} registros")

    # Páginas adicionales
    for page in range(2, total_pages + 1):
        log(f"Página {page} / {total_pages}...")
        result = call_module(
            base_url, jwt, module_name, module_cfg, args,
            page=page,
            clear_first_page=False,   # no limpiar en páginas siguientes
        )
        if result is None:
            log(f"Error en página {page}, se detiene paginación.", "WARN")
            break

        msg = result.get("message", "")
        saved_total = sum(s["total"] for s in result.get("detail", {}).get("saved", []))
        log(f"{msg}  (+{saved_total} registros)", "OK")


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Sincroniza módulos de Siigo contra la ESuite Account API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Conexión
    conn = parser.add_argument_group("Conexión")
    conn.add_argument("--url", default=None, help="URL base de la API (ej: http://localhost:8000)")
    conn.add_argument("--username", default=None, help="Usuario ESuite")
    conn.add_argument("--password", default=None, help="Contraseña ESuite")
    conn.add_argument("--key", default=None, type=int, help="ID del cliente (cliente_id)")

    # Siigo
    siigo = parser.add_argument_group("Credenciales Siigo")
    siigo.add_argument("--siigo-username", help="Usuario de Siigo (email)")
    siigo.add_argument("--siigo-access-key", help="Access key de Siigo")
    siigo.add_argument(
        "--skip-token",
        action="store_true",
        help="Omitir la generación del token Siigo (usar el token ya almacenado)",
    )

    # Módulos
    mods = parser.add_argument_group("Módulos")
    mods.add_argument(
        "--modules",
        nargs="+",
        metavar="MODULO",
        help="Módulos a sincronizar (ej: invoices customers products)",
    )
    mods.add_argument(
        "--all",
        action="store_true",
        help="Sincronizar todos los módulos disponibles",
    )
    mods.add_argument(
        "--list-modules",
        action="store_true",
        help="Listar módulos disponibles y salir",
    )

    # Rango de fechas
    dates = parser.add_argument_group("Rango de fechas (YYYY-MM-DD)")
    dates.add_argument("--date-start", metavar="FECHA", help="Fecha de inicio (created_start / date_start / due_date_start)")
    dates.add_argument("--date-end", metavar="FECHA", help="Fecha de fin (created_end / date_end / due_date_end)")

    # Balance report
    bal = parser.add_argument_group("Opciones de balance report")
    bal.add_argument("--year", type=int, help="Año del reporte (requerido para balance-report)")
    bal.add_argument("--month", type=int, metavar="1-12", help="Mes del reporte")
    bal.add_argument("--month-start", type=int, metavar="1-12", help="Mes inicio del rango")
    bal.add_argument("--month-end", type=int, metavar="1-12", help="Mes fin del rango")
    bal.add_argument("--account-start", metavar="CUENTA", help="Cuenta contable inicio")
    bal.add_argument("--account-end", metavar="CUENTA", help="Cuenta contable fin")
    bal.add_argument(
        "--includes-tax-difference",
        action="store_true",
        default=None,
        help="Incluir diferencia de impuestos en balance report",
    )

    # Document types
    parser.add_argument("--doc-type", metavar="TIPO", help="Tipo de documento para document-types (ej: FV)")

    # Paginación
    parser.add_argument("--page-size", type=int, default=100, metavar="N", help="Registros por página (default: 100, max: 100)")

    return parser.parse_args()


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────

def main():
    args = parse_args()

    if args.list_modules:
        print("\nMódulos disponibles:\n")
        for name, cfg in MODULES.items():
            date_info = f"  [fechas: {cfg['date_fields']}]" if cfg["date_fields"] else ""
            print(f"  {name:<35} {cfg['description']}{date_info}")
        print()
        sys.exit(0)

    if not args.url or not args.username or not args.password or args.key is None:
        print("ERROR: --url, --username, --password y --key son requeridos.")
        sys.exit(1)

    if not args.all and not args.modules:
        print("ERROR: especifica --modules o usa --all para sincronizar todos.\n")
        print("Módulos disponibles:", ", ".join(ALL_MODULES))
        sys.exit(1)

    selected = ALL_MODULES if args.all else args.modules

    invalid = [m for m in selected if m not in MODULES]
    if invalid:
        print(f"ERROR: módulos no reconocidos: {', '.join(invalid)}")
        print("Módulos disponibles:", ", ".join(ALL_MODULES))
        sys.exit(1)

    if not args.skip_token and (not args.siigo_username or not args.siigo_access_key):
        print("ERROR: --siigo-username y --siigo-access-key son requeridos (o usa --skip-token).")
        sys.exit(1)

    base_url = args.url.rstrip("/")

    print(f"\n{'═' * 55}")
    print(f"  SYNC SIIGO  —  cliente_id={args.key}")
    print(f"  URL: {base_url}")
    print(f"  Módulos: {', '.join(selected)}")
    if args.date_start or args.date_end:
        print(f"  Rango: {args.date_start or '(sin inicio)'} → {args.date_end or '(sin fin)'}")
    print(f"{'═' * 55}")

    jwt = login(base_url, args.username, args.password, args.key)

    if not args.skip_token:
        generate_siigo_token(base_url, jwt, args.siigo_username, args.siigo_access_key)

    start_time = datetime.now()

    for module_name in selected:
        sync_module(base_url, jwt, module_name, args)

    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n{'═' * 55}")
    print(f"  Sincronización completada en {elapsed:.1f}s")
    print(f"{'═' * 55}\n")


if __name__ == "__main__":
    main()
