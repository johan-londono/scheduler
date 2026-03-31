"""
Configuración del servicio de reenvío automático.
Las variables se inyectan desde scheduler_credentials via env_config.
"""
import os

# Conexión a la DB principal de esuite (donde están registrados los clientes)
# Todas estas variables deben estar configuradas en scheduler_credentials
MAIN_DB_HOST     = os.environ['MAIN_DB_HOST']
MAIN_DB_NAME     = os.environ['MAIN_DB_NAME']
MAIN_DB_USER     = os.environ['MAIN_DB_USER']
MAIN_DB_PASSWORD = os.environ['MAIN_DB_PASSWORD']
MAIN_DB_PORT     = int(os.environ['MAIN_DB_PORT'])

# API de emisión DIAN
API_PYTHON_URL      = os.environ['API_PYTHON_URL'].rstrip('/')
API_PYTHON_USERNAME = os.environ['API_PYTHON_USERNAME']
API_PYTHON_PASSWORD = os.environ['API_PYTHON_PASSWORD']

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
