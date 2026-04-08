"""
Configuración del servicio de reenvío automático.
Las variables de DB principal son requeridas y deben inyectarse desde
scheduler_credentials vía env_config (la tarea Celery las pone en os.environ
antes de lanzar el subprocess). Las vars SCHEDULER_DB_* ya están en el
entorno del worker y se heredan automáticamente.
"""
import os

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
FILTRO_DIA   = os.getenv('FILTRO_DIA',   '').strip()
FILTRO_MES   = os.getenv('FILTRO_MES',   '').strip()
FILTRO_ANIO  = os.getenv('FILTRO_ANIO',  '').strip()
FILTRO_DESDE = os.getenv('FILTRO_DESDE', '').strip()
FILTRO_HASTA = os.getenv('FILTRO_HASTA', '').strip()
