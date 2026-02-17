#!/usr/bin/env python3
"""
Script de sincronización con Dominus API via ESuite.

Endpoints soportados:
  - invoices:     POST /api/v1/dominus/generate/invoices
  - consolidated: POST /api/v1/dominus/generate/consolidated

Uso:
  python3 dominus_script.py --customer 26 --branch 1054 \
      -ds 2026-02-01 -de 2026-02-17 -ps invoices

  python3 dominus_script.py --customer 26 --branch 1054 \
      -ds 2026-02-01 -de 2026-02-17 -ps consolidated

  python3 dominus_script.py --customer 26 --branch 1054 \
      -ds 2026-02-01 -de 2026-02-17 -ps all
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dominus_script")

PROCESOS_DISPONIBLES = ["invoices", "consolidated"]

# Umbral en segundos: si un chunk tarda más, se reduce el intervalo
UMBRAL_TIEMPO_CHUNK = 120  # 2 minutos

# ── Configuración desde entorno ──────────────────────────────────────────────

API_URL = os.getenv("DOMINUS_API_URL", "https://api.esuite.com")
ESUITE_USER = os.getenv("DOMINUS_ESUITE_USER", "")
ESUITE_PASS = os.getenv("DOMINUS_ESUITE_PASSWORD", "")
DOMINUS_CLIENT_ID = os.getenv("DOMINUS_CLIENT_ID", "")
DOMINUS_CLIENT_SECRET = os.getenv("DOMINUS_CLIENT_SECRET", "")


class DominusClient:
    """Cliente para interactuar con la API Dominus a través de ESuite."""

    def __init__(self, api_url: str, customer_id: int, branch_id: int):
        self.api_url = api_url.rstrip("/")
        self.customer_id = customer_id
        self.branch_id = branch_id
        self.token = None
        self.token_time = None
        self.session = requests.Session()

    def autenticar(self) -> str:
        """Obtiene token de acceso de ESuite."""
        url = f"{self.api_url}/api/token/"
        data = {
            "username": ESUITE_USER,
            "password": ESUITE_PASS,
            "key": str(self.customer_id),
        }
        logger.info(f"Autenticando en ESuite para customer {self.customer_id}...")
        resp = self.session.post(url, data=data)
        resp.raise_for_status()
        body = resp.json()
        self.token = body.get("access_token")
        if not self.token:
            raise ValueError(f"No se obtuvo access_token: {body}")
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        self.token_time = time.time()
        logger.info("Autenticación exitosa.")
        return self.token

    def _renovar_si_expira(self):
        """Re-autentica si el token tiene más de 20 minutos."""
        if self.token_time and (time.time() - self.token_time) > 1200:
            logger.info("Token próximo a expirar, renovando...")
            self.autenticar()
            if DOMINUS_CLIENT_ID and DOMINUS_CLIENT_SECRET:
                self.generar_token_dominus()

    def generar_token_dominus(self) -> dict:
        """Genera/renueva el token de Dominus almacenado en ESuite."""
        url = f"{self.api_url}/api/v1/dominus/generate/token"
        payload = {
            "client_id": DOMINUS_CLIENT_ID,
            "client_secret": DOMINUS_CLIENT_SECRET,
            "grant_type": "client_credentials",
            "scope": "dominus",
        }
        logger.info("Generando token de Dominus...")
        resp = self.session.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        logger.info(f"Token Dominus: {body.get('message', 'OK')}")
        return body

    def _post_con_reintento(self, url: str, payload: dict, use_json: bool = False, timeout: int = 600) -> requests.Response:
        """Ejecuta POST con re-autenticación automática si da 401."""
        send_kwargs = {"json": payload} if use_json else {"data": payload}
        resp = self.session.post(url, timeout=timeout, **send_kwargs)
        if resp.status_code == 401:
            logger.warning("Token expirado (401), re-autenticando...")
            self.autenticar()
            if DOMINUS_CLIENT_ID and DOMINUS_CLIENT_SECRET:
                self.generar_token_dominus()
            resp = self.session.post(url, timeout=timeout, **send_kwargs)
        return resp

    def sincronizar_invoices(self, start_date: str, end_date: str, clear: bool = True, intervalo_dias: int = 2) -> dict:
        """Sincroniza facturas con evaluador adaptativo de chunks."""
        url = f"{self.api_url}/api/v1/dominus/generate/invoices"
        dt_start = datetime.strptime(start_date, "%Y-%m-%d")
        dt_end = datetime.strptime(end_date, "%Y-%m-%d")

        # Si es un solo día, ejecutar directo
        if dt_start == dt_end:
            self._renovar_si_expira()
            payload = {
                "branch_id": self.branch_id,
                "start_date": start_date,
                "end_date": end_date,
                "clear": clear,
            }
            logger.info(f"Sincronizando invoices: {start_date} (branch={self.branch_id}, clear={clear})")
            resp = self._post_con_reintento(url, payload, timeout=1800)
            resp.raise_for_status()
            body = resp.json()
            self._log_resultado("invoices", body)
            return body

        # Dividir en intervalos adaptativos
        total_dias = (dt_end - dt_start).days + 1
        total_chunks_estimados = -(-total_dias // intervalo_dias)  # ceil division
        logger.info(
            f"Dividiendo invoices en chunks adaptativos: {start_date} → {end_date} "
            f"({total_dias} días, ~{total_chunks_estimados} chunks de {intervalo_dias}d)"
        )
        total_results = 0
        all_saved = {}
        current = dt_start
        first_chunk = True
        chunk_size = intervalo_dias
        chunk_num = 0

        while current <= dt_end:
            self._renovar_si_expira()
            chunk_num += 1

            chunk_end = min(current + timedelta(days=chunk_size - 1), dt_end)
            cs = current.strftime("%Y-%m-%d")
            ce = chunk_end.strftime("%Y-%m-%d")
            dias_restantes = (dt_end - current).days + 1

            payload = {
                "branch_id": self.branch_id,
                "start_date": cs,
                "end_date": ce,
                "clear": clear if first_chunk else False,
            }
            logger.info(
                f"  [Chunk {chunk_num}] {cs} → {ce} "
                f"(size={chunk_size}d, restantes={dias_restantes}d, clear={payload['clear']})"
            )

            t_chunk = time.time()
            try:
                resp = self._post_con_reintento(url, payload, timeout=600)
                resp.raise_for_status()
            except requests.Timeout:
                # Timeout: si estamos en chunks > 1 día, reducir y reintentar
                elapsed = time.time() - t_chunk
                if chunk_size > 1:
                    chunk_size = 1
                    logger.warning(
                        f"  Timeout en chunk {cs}→{ce} ({elapsed:.0f}s). "
                        f"Reduciendo a chunks de 1 día y reintentando..."
                    )
                    continue  # Reintentar desde 'current' con chunk_size=1
                else:
                    raise  # Ya estamos en 1 día, propagar el error

            elapsed = time.time() - t_chunk
            body = resp.json()
            detail = body.get("detail", {})
            total_results += detail.get("total_results", 0)
            for s in detail.get("saved", []):
                table = s.get("table", "?")
                all_saved[table] = all_saved.get(table, 0) + s.get("total", 0)

            chunk_results = detail.get("total_results", 0)
            self._log_resultado("invoices", body)
            logger.info(f"  [Chunk {chunk_num}] Completado en {elapsed:.1f}s ({chunk_results} registros) | Acumulado: {total_results}")

            # Evaluador: si tardó más del umbral, reducir chunks
            if elapsed > UMBRAL_TIEMPO_CHUNK and chunk_size > 1:
                chunk_size = 1
                logger.warning(f"  Chunk tardó {elapsed:.0f}s (>{UMBRAL_TIEMPO_CHUNK}s). Reduciendo a chunks de 1 día.")

            first_chunk = False
            current = chunk_end + timedelta(days=1)

        combined = {
            "status": 200,
            "message": f"Se agregaron {total_results} registros (chunks adaptativos).",
            "detail": {
                "total_results": total_results,
                "saved": [{"table": t, "total": c} for t, c in all_saved.items()],
            },
        }
        logger.info(f"[invoices] Total combinado: {total_results} resultados")
        return combined

    def sincronizar_consolidated(self, start_date: str, end_date: str, clear: bool = True) -> dict:
        """Sincroniza consolidados desde Dominus."""
        self._renovar_si_expira()
        url = f"{self.api_url}/api/v1/dominus/generate/consolidated"
        # consolidated usa 'final_date' en lugar de 'end_date'
        payload = {
            "branch_id": self.branch_id,
            "start_date": start_date,
            "final_date": end_date,
            "clear": clear,
        }
        logger.info(f"Sincronizando consolidated: {start_date} → {end_date} (branch={self.branch_id}, clear={clear})")
        resp = self._post_con_reintento(url, payload, timeout=1800)
        resp.raise_for_status()
        body = resp.json()
        self._log_resultado("consolidated", body)
        return body

    def _log_resultado(self, proceso: str, body: dict):
        """Muestra el resultado de una sincronización."""
        status = body.get("status", "?")
        message = body.get("message", "")
        detail = body.get("detail", {})
        elapsed = detail.get("elapsed_seconds", "?")
        total = detail.get("total_results", "?")
        saved = detail.get("saved", [])

        logger.info(f"[{proceso}] Status={status} | {message}")
        logger.info(f"[{proceso}] Tiempo={elapsed}s | Total resultados={total}")
        for tabla in saved:
            logger.info(f"  └─ {tabla.get('table', '?')}: {tabla.get('total', 0)} registros")


def validar_fecha(valor: str) -> str:
    """Valida formato YYYY-MM-DD."""
    try:
        datetime.strptime(valor, "%Y-%m-%d")
        return valor
    except ValueError:
        raise argparse.ArgumentTypeError(f"Fecha inválida: '{valor}'. Use formato YYYY-MM-DD.")


def main():
    parser = argparse.ArgumentParser(description="Sincronización Dominus API via ESuite")
    parser.add_argument("--customer", type=int, required=True, help="Customer ID en ESuite")
    parser.add_argument("--branch", type=int, required=True, help="Branch ID en Dominus")
    parser.add_argument("-ds", "--date-start", type=validar_fecha, required=True, help="Fecha inicio (YYYY-MM-DD)")
    parser.add_argument("-de", "--date-end", type=validar_fecha, required=True, help="Fecha fin (YYYY-MM-DD)")
    parser.add_argument(
        "-ps",
        "--process",
        required=True,
        choices=PROCESOS_DISPONIBLES + ["all"],
        help="Proceso a ejecutar (invoices, consolidated, all)",
    )
    parser.add_argument("--no-clear", action="store_true", help="No limpiar datos previos del rango")
    parser.add_argument("--skip-token", action="store_true", help="Omitir generación de token Dominus")

    args = parser.parse_args()

    if not ESUITE_USER or not ESUITE_PASS:
        logger.error("Variables DOMINUS_ESUITE_USER y DOMINUS_ESUITE_PASSWORD son requeridas.")
        sys.exit(1)

    client = DominusClient(API_URL, args.customer, args.branch)
    clear = not args.no_clear
    procesos_preview = PROCESOS_DISPONIBLES if args.process == "all" else [args.process]

    logger.info("=" * 60)
    logger.info("INICIO SINCRONIZACIÓN DOMINUS")
    logger.info(f"  API:      {API_URL}")
    logger.info(f"  Customer: {args.customer} | Branch: {args.branch}")
    logger.info(f"  Rango:    {args.date_start} → {args.date_end}")
    logger.info(f"  Procesos: {', '.join(procesos_preview)}")
    logger.info(f"  Clear:    {clear}")
    logger.info("=" * 60)

    t_global = time.time()

    try:
        # ── Paso 1/4: Autenticación ESuite ───────────────────────────
        logger.info("=" * 60)
        logger.info("[PASO 1/4] Autenticación en ESuite")
        logger.info("=" * 60)
        client.autenticar()

        # ── Paso 2/4: Token Dominus ──────────────────────────────────
        logger.info("=" * 60)
        logger.info("[PASO 2/4] Generación de token Dominus")
        logger.info("=" * 60)
        if not args.skip_token:
            if DOMINUS_CLIENT_ID and DOMINUS_CLIENT_SECRET:
                client.generar_token_dominus()
            else:
                logger.warning("DOMINUS_CLIENT_ID / DOMINUS_CLIENT_SECRET no configurados. Omitiendo.")
        else:
            logger.info("Omitido (--skip-token)")

        # ── Paso 3/4: Ejecutar procesos ──────────────────────────────
        procesos = PROCESOS_DISPONIBLES if args.process == "all" else [args.process]
        total_procesos = len(procesos)
        resultados = {}

        for i, proceso in enumerate(procesos, 1):
            logger.info("=" * 60)
            logger.info(f"[PASO 3/4] Proceso {i}/{total_procesos}: {proceso.upper()}")
            logger.info(f"  Rango: {args.date_start} → {args.date_end}")
            logger.info("=" * 60)

            t0 = time.time()
            try:
                if proceso == "invoices":
                    resp = client.sincronizar_invoices(args.date_start, args.date_end, clear)
                elif proceso == "consolidated":
                    resp = client.sincronizar_consolidated(args.date_start, args.date_end, clear)
                else:
                    logger.error(f"Proceso desconocido: {proceso}")
                    continue

                elapsed = round(time.time() - t0, 2)
                logger.info(f"[{proceso}] Completado en {elapsed}s")
                resultados[proceso] = {"estado": "OK", "respuesta": resp, "tiempo": elapsed}
            except requests.HTTPError as e:
                error_body = ""
                if e.response is not None:
                    try:
                        error_body = e.response.text[:500]
                    except Exception:
                        pass
                elapsed = round(time.time() - t0, 2)
                logger.error(f"Error HTTP en {proceso} ({elapsed}s): {e} | {error_body}")
                resultados[proceso] = {"estado": "ERROR", "detalle": str(e), "tiempo": elapsed}
            except requests.Timeout:
                elapsed = round(time.time() - t0, 2)
                logger.error(f"Timeout en {proceso} ({elapsed}s)")
                resultados[proceso] = {"estado": "ERROR", "detalle": "Timeout", "tiempo": elapsed}

        # ── Paso 4/4: Resumen ─────────────────────────────────────────
        tiempo_total = round(time.time() - t_global, 2)
        logger.info("=" * 60)
        logger.info(f"[PASO 4/4] RESUMEN DE SINCRONIZACIÓN DOMINUS")
        logger.info(f"  Customer: {args.customer} | Branch: {args.branch}")
        logger.info(f"  Rango: {args.date_start} → {args.date_end}")
        logger.info(f"  Tiempo total: {tiempo_total}s")
        logger.info("-" * 60)
        hay_errores = False
        for proc, res in resultados.items():
            estado = res["estado"]
            tiempo = res["tiempo"]
            if estado == "OK":
                total = res.get("respuesta", {}).get("detail", {}).get("total_results", "?")
                logger.info(f"  {proc}: OK ({total} resultados, {tiempo}s)")
            else:
                hay_errores = True
                logger.info(f"  {proc}: ERROR ({res.get('detalle', '?')}, {tiempo}s)")
        logger.info("=" * 60)

        # Imprimir JSON del resumen para que la tarea Celery lo capture
        print(json.dumps(resultados, default=str))

        if hay_errores:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error fatal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
