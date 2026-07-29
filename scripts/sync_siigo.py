"""Librería para sincronizar datos desde la API de Siigo.

No es ejecutable: se instancia SiigoSync una vez con la config y las
credenciales del cliente, y se llama run_sync() por rango/proceso. La config,
el customer y las credenciales viven en la instancia; ninguna función interna
las recibe como parámetro.

    from scripts.sync_siigo import Config, SiigoSync

    sync = SiigoSync(
        customer=23,
        config=Config(api_url=..., api_user=..., api_password=...),
        username="usuario@empresa.com",
        access_key="...",
    )
    resultado = await sync.run_sync(
        date_start="2026-07-01",
        date_end="2026-07-31",
        process="customers",   # opcional; None ejecuta todos
    )
"""

import os
import asyncio
import json
import logging
import httpx
from datetime import datetime, date
from dotenv import load_dotenv
from dataclasses import dataclass
from typing import Optional, Dict, Any

load_dotenv()

# ========== LOGGING ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== CONFIGURACIÓN ==========
@dataclass
class Config:
    api_url: str
    api_user: str
    api_password: str
    workers: int = 10
    page_size: int = 100


PROCESS = [
    'products', 'invoices', 'customers', 'accounts-payable',
    'balance-report', 'balance-report-by-thirdparty', 'purchases',
    'journals', 'users', 'document-types', 'credit-notes',
]

PROCESS_DATE_RANGE = {
    'products': 'created',
    'invoices': 'created',
    'customers': 'created',
    'credit-notes': 'created',
    'accounts-payable': 'due_date',
}

# ========== FUNCIONES DE VALIDACIÓN ==========

def parse_date(date_str: str, field: str = "fecha") -> date:
    """Convierte YYYY-MM-DD a date. Lanza ValueError con mensaje claro."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise ValueError(f"{field} inválida: '{date_str}' (formato esperado: YYYY-MM-DD)")

def is_valid_json_response(response_data: Dict[str, Any]) -> bool:
    """Valida estructura mínima de respuesta de API."""
    if not isinstance(response_data, dict):
        return False
    required_keys = ['detail']
    return all(key in response_data for key in required_keys)

# ========== API PÚBLICA ==========

class SiigoSync:
    """Todo el flujo vive aquí: config, customer y credenciales se leen de
    self, no se pasan como parámetros entre funciones."""

    def __init__(self, customer: int, config: Config, username: str, access_key: str):
        if not config.api_url or not isinstance(config.api_url, str):
            raise ValueError(f"api_url es obligatorio, recibido: {config.api_url!r}")
        if not isinstance(customer, int) or isinstance(customer, bool) or customer <= 0:
            raise ValueError(f"customer debe ser un entero positivo, recibido: {customer!r}")
        if not username or not username.strip():
            raise ValueError("username es obligatorio")
        if not access_key or not access_key.strip():
            raise ValueError("access_key es obligatorio")

        self.customer = customer
        self.config = config
        self.username = username
        self.access_key = access_key

    # ========== API ==========

    async def _generate_api_token(self, http_client: httpx.AsyncClient) -> Optional[str]:
        """Genera token de autenticación (httpx async)."""
        try:
            data = {
                'username': self.config.api_user,
                'password': self.config.api_password,
                'key': self.customer,
            }

            # La barra final evita el 307 de FastAPI (/token -> /token/)
            response = await http_client.post(
                f"{self.config.api_url}/token/",
                data=data,
                headers={'Accept': 'application/json'}
            )

            if response.status_code != 200:
                logger.error(f"Error generando token: {response.status_code} - {response.text}")
                return None

            token = response.json().get('access_token')
            if not token:
                logger.error("Token no encontrado en respuesta")
                return None

            return f"Bearer {token}"
        except Exception as e:
            logger.error(f"Excepción en _generate_api_token: {e}", exc_info=True)
            return None

    async def _generate_siigo_token(
        self, http_client: httpx.AsyncClient, api_token: str
    ) -> Optional[str]:
        """Abre la sesión Siigo en el backend con las credenciales del cliente.

        Requiere el Bearer de /token/ y manda username/access_key como JSON.
        El backend guarda la sesión; los endpoints generate/* siguen usando el
        Bearer de la API, no lo que devuelve esta función.
        """
        try:
            response = await http_client.post(
                f"{self.config.api_url}/v1/siigo/generate/token",
                json={
                    'username': self.username,
                    'access_key': self.access_key,
                },
                headers={
                    'Accept': 'application/json',
                    'Authorization': api_token,
                }
            )

            if response.status_code != 200:
                logger.error(
                    f"Error generando token Siigo: {response.status_code} - {response.text[:500]}"
                )
                return None

            return response.json().get('message')

        except Exception as e:
            logger.error(f"Excepción en _generate_siigo_token: {e}", exc_info=True)
            return None

    async def _request_api(
        self,
        http_client: httpx.AsyncClient,
        process: str,
        page: int,
        date_start: str,
        date_end: str,
        clear: bool = True
    ) -> Optional[Dict]:
        """Realiza request a API de Siigo con validación de respuesta.

        date_start/date_end llegan siempre como YYYY-MM-DD; la conversión a ISO
        con hora se hace aquí para que ambas ramas puedan reparsear la fecha.
        """
        try:
            token = await self._generate_api_token(http_client)

            if not token:
                logger.error("No se pudo obtener token de autenticación")
                return None

            # Preparar datos según tipo de proceso
            if process in PROCESS_DATE_RANGE:
                suffix = PROCESS_DATE_RANGE[process]
                data = {
                    f"{suffix}_start": f"{date_start}T00:00:00",
                    f"{suffix}_end": f"{date_end}T23:59:59",
                }
            else:
                date_obj = datetime.strptime(date_start, "%Y-%m-%d")
                data = {
                    'month': date_obj.month,
                    'year': date_obj.year
                }

            response = await http_client.post(
                f"{self.config.api_url}/v1/siigo/generate/{process}",
                params={
                    'page': page,
                    'pageSize': self.config.page_size,
                    'clear': str(clear).lower()
                },
                data=data,
                headers={
                    'Accept': 'application/json',
                    'Authorization': token
                }
            )

            logger.info(f"[{process}] Request: página {page}")

            if response.status_code != 200:
                logger.error(
                    f"[{process}] página {page}: HTTP {response.status_code} - {response.text[:500]}"
                )
                return None

            try:
                response_json = response.json()
            except json.JSONDecodeError:
                logger.error(f"Respuesta no es JSON válida: {response.text[:500]}")
                return None

            if not is_valid_json_response(response_json):
                logger.error(f"Respuesta no tiene estructura esperada: {response_json}")
                return None

            details = response_json.get('detail', {})

            # Usar .get() para evitar KeyError
            params = {
                'data': data,
                'page': details.get('page', page),
                'pages': details.get('pages', 1),
                'page_size': details.get('page_size', self.config.page_size),
                'module': process,
                'execute_date': details.get('execute_date'),
                'end_date': details.get('end_date'),
                'total_results': details.get('total_results'),
                'saved': details.get('saved')
            }

            logger.info(
                f"[{process}] Éxito: página {page}/{details.get('pages', 1)} "
                f"({details.get('total_results')} resultados)"
            )

            return params

        except Exception as e:
            logger.error(f"Excepción en _request_api: {e}", exc_info=True)
            return None

    # ========== PROCESAMIENTO ==========

    async def _seed_process(
        self,
        http_client: httpx.AsyncClient,
        process: str,
        date_start: str,
        date_end: str,
        tasks_queue: asyncio.Queue,
        page: int = 1,
        unique: bool = False
    ) -> Optional[Dict]:
        """Ejecuta la página 1 de forma síncrona y encola las páginas restantes.

        La primera petición es obligatoriamente bloqueante: su respuesta trae
        'pages', el total que hay que recorrer. Sin ese dato no se sabe cuántas
        tareas encolar.

        Devuelve el resultado de la página 1, o None si falló.
        """
        clear = not unique

        result = await self._request_api(
            http_client, process, page, date_start, date_end, clear
        )

        if result is None:
            logger.warning(f"[{process}] página {page} falló; no se encolan páginas adicionales")
            return None

        if unique:
            return result

        pages = result.get('pages') or 1

        for next_page in range(page + 1, pages + 1):
            # clear=False: solo la página 1 limpia los datos previos
            await tasks_queue.put(
                (process, next_page, date_start, date_end, False)
            )

        if pages > page:
            logger.info(f"[{process}] {pages - page} páginas encoladas (total {pages})")

        return result

    async def _process_queue(self, tasks_queue: asyncio.Queue) -> None:
        """Procesa cola de tareas con N workers."""

        async def worker(worker_id: int):
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http_client:
                while True:
                    try:
                        task = await asyncio.wait_for(tasks_queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        break

                    try:
                        await self._request_api(http_client, *task)
                    except Exception as e:
                        logger.error(f"Worker {worker_id} error: {e}", exc_info=True)
                    finally:
                        tasks_queue.task_done()

        workers = [
            asyncio.create_task(worker(i))
            for i in range(self.config.workers)
        ]

        await tasks_queue.join()

        for w in workers:
            w.cancel()

        logger.info("Todas las tareas procesadas")

    async def run_sync(
        self,
        date_start: str,
        date_end: str,
        process: Optional[str] = None,
        page: Optional[int] = None,
        unique: bool = False,
    ) -> Dict[str, Any]:
        import time
        start_time = time.time()

        parsed_start = parse_date(date_start, "date_start")
        parsed_end = parse_date(date_end, "date_end")
        if parsed_start > parsed_end:
            raise ValueError(f"date_start ({date_start}) no puede ser posterior a date_end ({date_end})")

        if process is not None and process not in PROCESS:
            raise ValueError(f"process inválido: '{process}'. Opciones: {', '.join(PROCESS)}")

        if page is not None and (not isinstance(page, int) or page < 1):
            raise ValueError(f"page debe ser un entero >= 1, recibido: {page!r}")

        start_page = page or 1
        process_list = [process] if process else list(PROCESS)

        logger.info(f"Iniciando sincronización cliente={self.customer} procesos={len(process_list)}")

        tasks_queue = asyncio.Queue()
        ok, failed = [], []

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http_client:
            api_token = await self._generate_api_token(http_client)
            if not api_token:
                raise RuntimeError("No se pudo obtener el token de la API")

            siigo_token = await self._generate_siigo_token(http_client, api_token)
            if siigo_token is None:
                raise RuntimeError(f"No se pudo abrir la sesión Siigo para el cliente {self.customer}")

            logger.info("Sesión Siigo establecida")

            for proc in process_list:
                result = await self._seed_process(
                    http_client, proc, date_start, date_end, tasks_queue,
                    page=start_page, unique=unique
                )
                (ok if result is not None else failed).append(proc)

        queued = tasks_queue.qsize()

        if queued:
            await self._process_queue(tasks_queue)
        else:
            logger.info("No hay páginas adicionales que procesar")

        elapsed = time.time() - start_time
        logger.info(f"Sincronización terminada en {elapsed:.2f}s (ok={len(ok)} fallidos={len(failed)})")

        return {
            'customer': self.customer,
            'processes': process_list,
            'ok': ok,
            'failed': failed,
            'queued': queued,
            'elapsed': elapsed,
        }