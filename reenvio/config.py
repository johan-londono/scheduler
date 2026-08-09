"""
Configuración del servicio de reenvío automático.
Las variables de DB principal son requeridas y deben inyectarse desde
scheduler_credentials vía env_config (la tarea Celery las pone en os.environ
antes de lanzar el subprocess). Las vars SCHEDULER_DB_* ya están en el
entorno del worker y se heredan automáticamente.
"""
import os
import re

# Requeridas — deben venir de scheduler_credentials vía env_config
MAIN_DB_HOST     = os.environ['MAIN_DB_HOST']
MAIN_DB_NAME     = os.environ['MAIN_DB_NAME']
MAIN_DB_USER     = os.environ['MAIN_DB_USER']
MAIN_DB_PASSWORD = os.environ['MAIN_DB_PASSWORD']
MAIN_DB_PORT     = int(os.environ['MAIN_DB_PORT'])

# API de emisión DIAN
API_PYTHON_URL      = os.environ['API_PYTHON_URL'].rstrip('/')
API_PYTHON_USERNAME = os.environ['API_PYTHON_USERNAME']
API_PYTHON_PASSWORD = os.environ['API_PYTHON_PASSWORD']

# Scheduler DB — ya en el entorno del worker; fallback a MAIN_DB si coincide
SCHEDULER_DB_HOST     = os.getenv('SCHEDULER_DB_HOST',     MAIN_DB_HOST)
SCHEDULER_DB_NAME     = os.getenv('SCHEDULER_DB_DATABASE', 'programadador_tareas')
SCHEDULER_DB_USER     = os.getenv('SCHEDULER_DB_USER',     MAIN_DB_USER)
SCHEDULER_DB_PASSWORD = os.getenv('SCHEDULER_DB_PASSWORD', MAIN_DB_PASSWORD)
SCHEDULER_DB_PORT     = int(os.getenv('SCHEDULER_DB_PORT', str(MAIN_DB_PORT)))


# Proveedor de integración DIAN
PROVEEDOR_INTEGRACION = os.getenv('PROVEEDOR_INTEGRACION', 'AVIA')

# Máximo de intentos de envío por factura
MAX_INTENTOS = int(os.getenv('MAX_INTENTOS', '3'))

# Filtrar un cliente específico (vacío = procesar todos)
KEY_CLI_FILTER = os.getenv('KEY_CLI_FILTER', '').strip()

# Filtros de fecha (vacío = mes actual por defecto)
# Se puede usar uno de los siguientes modos:
#   FILTRO_DIA   = YYYY-MM-DD          (día específico)
#   FILTRO_MES   = YYYY-MM             (mes específico)
#   FILTRO_ANIO  = YYYY                (año específico)
#   FILTRO_DESDE + FILTRO_HASTA        (rango personalizado YYYY-MM-DD)
def _filtro(nombre: str, patron: str) -> str:
    """Lee un FILTRO_* validando su formato.

    Estos valores se embeben en el SQL que corre contra la DB de cada cliente y
    ya no vienen solo del .env: llegan de scheduler_credentials, editable por
    cualquier admin de la API. Validar el formato cierra la vía de inyección.
    """
    valor = os.getenv(nombre, '').strip()
    if valor and not re.fullmatch(patron, valor):
        raise ValueError(f"{nombre} con formato inválido: {valor!r} (esperado {patron})")
    return valor


FILTRO_DIA   = _filtro('FILTRO_DIA',   r'\d{4}-\d{2}-\d{2}')
FILTRO_MES   = _filtro('FILTRO_MES',   r'\d{4}-\d{2}')
FILTRO_ANIO  = _filtro('FILTRO_ANIO',  r'\d{4}')
FILTRO_DESDE = _filtro('FILTRO_DESDE', r'\d{4}-\d{2}-\d{2}')
FILTRO_HASTA = _filtro('FILTRO_HASTA', r'\d{4}-\d{2}-\d{2}')


def filtro_fecha_sql(col: str) -> str:
    """
    Retorna la cláusula AND de fecha según los FILTRO_* activos.
    Sin ningún filtro configurado aplica el mes actual por defecto.

    El formato de cada valor ya se validó en _filtro().
    """
    if FILTRO_DIA:
        return f"AND DATE({col}) = '{FILTRO_DIA}'"
    if FILTRO_MES:
        return f"AND TO_CHAR({col}, 'YYYY-MM') = '{FILTRO_MES}'"
    if FILTRO_ANIO:
        return f"AND EXTRACT(YEAR FROM {col}) = {int(FILTRO_ANIO)}"
    if FILTRO_DESDE and FILTRO_HASTA:
        return f"AND DATE({col}) BETWEEN '{FILTRO_DESDE}' AND '{FILTRO_HASTA}'"
    # Default: mes en curso
    return f"AND DATE_TRUNC('month', {col}) = DATE_TRUNC('month', CURRENT_DATE)"
