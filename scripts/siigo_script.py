import os
import psycopg2
import asyncio
import json
import requests
import calendar
import copy
from argparse import ArgumentParser, ArgumentTypeError
# from rich_argparse import RichHelpFormatter
from rich import print
from datetime import datetime, time
from uuid import uuid4
import traceback
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor
from psycopg2 import OperationalError, InterfaceError, DatabaseError

load_dotenv()

## -- Credenciales -- ##
API_URL = os.getenv('API_SIIGO_URL')
API_USER = os.getenv('API_SIIGO_USER')
API_PASSWORD = os.getenv('API_SIIGO_PASSWORD')

DB_HOST = os.getenv('DB_HOST')
DB_DATABASE = os.getenv('DB_DATABASE')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_PORT = os.getenv('DB_PORT')

TASKS_ON_HOLD = asyncio.Queue()
CUSTOMER_ID = None

PROCESS = [
    'products',
    'invoices',
    'customers',
    'accounts-payable',
    'balance-report',
    'balance-report-by-thirdparty',
    'purchases',
    'journals',
    'users',
    'document-types',
    'credit-notes',
]

PROCESS_DATE_RANGE = {
    'products': 'created',
    'invoices': 'created',
    'customers': 'created',
    'credit-notes': 'created',
    'accounts-payable': 'due_date',
}

DOCUMENT_TYPES = [
    'FV', 'FC', 'NC', 'RC'
]


### ----- Start Schemas ----- ###
class Arguments():
    customer: float
    page: int | None = None
    unique: bool = False
    process: str | None = None
    date_start: str
    date_end: str

class SiigoCredential():
    id: float
    username: str
    access_key: str
    cliente_id: float
### ----- End Schemas ----- ###

### ----- Start Functions ----- ###


def dateValidation(date):
    try:
        datetime.strptime(date, "%Y-%m-%d").date()
        return date
    except ValueError:
        raise ArgumentTypeError(f"Invalid date: '{date}'")

def load_arguments():
    parser = ArgumentParser(
        description="[bold blue]Script para ejecutar peticiones a la API de Siigo[/bold blue]",
        # formatter_class=RichHelpFormatter
    )

    unique = False

    # Define los argumentos esperados
    parser.add_argument('-cu', '--customer', type=int, required=True, help='ID del cliente (entero positivo). Ejemplo: --customer [green]123[/green]')
    parser.add_argument('-ps', '--process', choices=PROCESS, metavar='PROCESS', type=str, required=False, help=f'Proceso a ejecutar.\n Opciones disponibles: {", ".join(PROCESS)}. Ejemplo: --process [green]"invoices"[/green]')
    parser.add_argument('-ds', '--date_start', type=dateValidation, required=True, help='Fecha de inicio en formato YYYY-MM-DD. Ejemplo: --date_start [green]2024-06-01[/green]')
    parser.add_argument('-de', '--date_end', type=dateValidation, required=True, help='Fecha de fin en formato YYYY-MM-DD. Ejemplo: --date_end [green]2024-06-30[/green]')
    parser.add_argument('-pg', '--page', type=int, required=False, help='Número de página a consultar (entero positivo). Ejemplo: --page [green]2[/green]')
    parser.add_argument('-u', '--unique', action='store_const', default=unique, const=not(unique), help='Ejecutar el proceso una sola vez (alternar entre True/False). Ejemplo: --unique')

    args: Arguments = parser.parse_args()

    if args.date_start > args.date_end:
        parser.error("La fecha de inicio (-ds) no puede ser posterior a la fecha de fin (-de).")

    dateStart = datetime.strptime(args.date_start, "%Y-%m-%d").date()
    dateEnd = datetime.strptime(args.date_end, "%Y-%m-%d").date()

    # if (dateStart.year != dateEnd.year):
    #     parser.error("La fecha de inicio (-ds) no puede ser de diferente año a la fecha de fin (-de).")

    return args

def connection_db():
    try:
        conn = psycopg2.connect(
            host = DB_HOST,
            dbname = DB_DATABASE,
            user = DB_USER,
            password = DB_PASSWORD,
            port = DB_PORT,
            cursor_factory=RealDictCursor
        )

        return conn

    except (OperationalError, InterfaceError, DatabaseError) as e:
        print(f"Error en la conexión a la base de datos: {e}")
        return None

async def get_siigo_credentials():
    try:
        cursor = connection_db().cursor()
        sql = "SELECT id, username, access_key, cliente_id FROM siigo_credentials WHERE cliente_id = '%s'" %(CUSTOMER_ID)
        cursor.execute(sql)
        return cursor.fetchone()
    finally:
        cursor.close()

async def generate_api_token():
    try:
        method = 'post'
        endpoint = f"{API_URL}/token"
        headers = { 'Accept' : 'application/json'}
        
        data = {
            'username': API_USER,
            'password': API_PASSWORD,
            'key': CUSTOMER_ID
        }

        response = requests.request(method, endpoint, headers = headers, data = data)
        responseJson = json.loads(response.text)
        
        if (response.status_code != 200):
            print(f"Error: ", {
                'function': 'generate_api_token',
                'response_code': response.status_code,
                'server_response': response.text
            })

            return None

        accessToken = responseJson['access_token']
        return f"Bearer {accessToken}"

    except Exception as e:
        print(e)
        return None

async def generate_siigo_api_token(token, siigoCredential):
    try:
        if siigoCredential is None:
            print("Credential not found.")
            return None

        method = 'post'
        endpoint = f"{API_URL}/v1/siigo/generate/token"
        headers = { 
            'Accept' : 'application/json',
            'Authorization': token
        }

        data = {
            'username': siigoCredential['username'],
            'access_key': siigoCredential['access_key'],
        }

        response = requests.request(method, endpoint, headers = headers, json = data)
        responseJson = json.loads(response.text)
        
        print(responseJson)
        
        if(response.status_code != 200):
            print(f"Error: ", {
                'function': 'generate_siigo_api_token',
                'response_code': response.status_code,
                'server_response': response.text
            })
            return None

        return responseJson['message']

    except Exception as e:
        print(e)
        return None

def is_json(text):
    try:
        jsonFormatter = json.loads(text)

        if isinstance(jsonFormatter, dict):
            return True

    except json.JSONDecodeError:
        return False

async def create_siigo_event(data = {}, uuid = uuid4()):
    try:

        db = connection_db()
        cursor = db.cursor()
        now = datetime.now()
        status = 5

        if(data['saved'] is None):
            status = 4

        sql = """
                INSERT INTO siigo_events (identifier, cliente_id, description, details, estado_id, created_at, updated_at) 
                VALUES('%s', %s, '%s', '%s', %s, '%s', '%s')
            """ %(uuid, CUSTOMER_ID, "Procesado desde el Script", json.dumps(data), status, now, now)

        cursor.execute(sql)
        db.commit()

        return uuid

    except (OperationalError, InterfaceError, DatabaseError) as e:
        print(f"Error function create_siigo_event(): {e}")
        return None

    finally:
        db.close()

async def request_api(args: Arguments, process, page, pageSize, clear, siigoCredential, uuid = None):

    try:
        token = await generate_api_token()
        # siigoToken = await generate_siigo_api_token(token, siigoCredential)

        # if(siigoToken is None):
        #     return None

        method = 'post'
        endpoint = API_URL + f"/v1/siigo/generate/{process}?page={page}&pageSize={pageSize}&clear={clear}"

        headers = { 
            'Accept' : 'application/json',
            'Authorization': token
        }

        data = {}
        params = {}
        details = None

        processDataRange = PROCESS_DATE_RANGE

        if process in processDataRange:
            suffix = processDataRange[process]
            data = {
                f"{suffix}_start": args.date_start,
                f"{suffix}_end": args.date_end,
            }
            print(f"page: {page}" )
        else:
            date = datetime.strptime(args.date_start, "%Y-%m-%d")
            data = {
                'month': date.month,
                'year': date.year
            }

        print(f"data sent: {data}")

        response = requests.request(method, endpoint, headers = headers, data = data)

        print("RESPONSE API")
        print(response.text)

        if (response.status_code != 200):
            responseBody = response.text

            isJson = is_json(response.text)

            print(isJson)

            if isJson:
                responseJson = json.loads(response.text)

                if responseJson.get('detail'):
                    responseJson = responseJson['detail']

                if responseJson.get('error'):
                    responseJson = responseJson['error']
                
                responseBody = responseJson

            print(f"Error: ", {
                'function': 'request_api',
                'process': process,
                'response_code': response.status_code,
                'server_response': responseBody
            })

            params = {
                'data': data,
                'page': page,
                'pages': None,
                'page_size': pageSize,
                'module': process,
                'execute_date': None,
                'end_date': None,
                'total_results': None,
                'saved': None,
                # 'details': responseBody
            }

        if (response.status_code == 200):
            responseJson = json.loads(response.text)

            details = responseJson['detail']
            params = {
                'data': data,
                'page': details['page'],
                'pages': details['pages'],
                'page_size': details['page_size'],
                'module': process,
                'execute_date': details['execute_date'],
                'end_date': details['end_date'],
                'total_results': details['total_results'],
                'saved': details['saved']
            }

        if uuid is None:
            uuid = await create_siigo_event(params)
            params['uuid'] = uuid
        else:
            await create_siigo_event(params, uuid)

        if details is not None:
            print({
                "customer_id": CUSTOMER_ID, 
                "process": process, 
                "page": page,
                "on_hold": details['pages'] - details['page']
            })

        if (response.status_code != 200):
            return None

        return params

    except Exception as e:
        error_trace = traceback.format_exc()
        print(f"Error function request_api(): {e}")
        print(f"Error details: {error_trace}")
        return None

async def request_process(args: Arguments, process, siigoCredential):
    global TASKS_ON_HOLD

    pageSize = 100
    balanceReportProcess = ['balance-report', 'balance-report-by-thirdparty']

    dateStart = datetime.strptime(args.date_start, "%Y-%m-%d").date()
    dateEnd = datetime.strptime(args.date_end, "%Y-%m-%d").date()

    args.date_start = datetime.combine(dateStart, time(0, 0, 0)).strftime("%Y-%m-%dT%H:%M:%S")
    args.date_end = datetime.combine(dateEnd, time(23, 59, 59)).strftime("%Y-%m-%dT%H:%M:%S")

    if process not in balanceReportProcess:

        page = args.page if args.page else 1
        clear = not args.unique
 
        response = await request_api(args, process, page, pageSize, clear, siigoCredential)

        if response is not None and response['pages'] > 1 and not args.unique:
            uuid = response['uuid']
            pages = response['pages']
        
            for i in range((page + 1), (pages + 1)):
                task_data = (args, process, i, pageSize, False, siigoCredential, uuid)
                await TASKS_ON_HOLD.put(task_data)

    if process in balanceReportProcess:

        # Parseo de fechas (solo fecha, sin hora)
        dateStart = datetime.strptime(args.date_start, "%Y-%m-%d").date()
        dateEnd = datetime.strptime(args.date_end, "%Y-%m-%d").date()

        clear = not args.unique

        while dateStart <= dateEnd:
            lastDay = calendar.monthrange(dateStart.year, dateStart.month)[1]

            argsCopy = copy.deepcopy(args)
            argsCopy.date_start = dateStart.strftime("%Y-%m-%dT%H:%M:%S")
            argsCopy.date_end = dateStart.replace(day=lastDay).strftime("%Y-%m-%dT%H:%M:%S")

            task_data = (argsCopy, process, 1, pageSize, clear, siigoCredential)
            
            await TASKS_ON_HOLD.put(task_data)

            if args.unique:
                break

            if dateStart.month == 12:
                dateStart = dateStart.replace(year=dateStart.year + 1, month=1)
            else:
                dateStart = dateStart.replace(month=dateStart.month + 1)

async def process_tasks():
    semaphore = asyncio.Semaphore(10)

    async def worker():
        while True:
            task_data = await TASKS_ON_HOLD.get()
            async with semaphore:
                await request_api(*task_data)
            TASKS_ON_HOLD.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(10)]

    await TASKS_ON_HOLD.join()

    for worker_task in workers:
        worker_task.cancel()

    print("All tasks have been processed")

### ----- End Functions ----- ###

async def main():
    global CUSTOMER_ID

    import time
    start_time = time.time()

    args: Arguments = load_arguments()
    CUSTOMER_ID = args.customer

    siigoCredential: SiigoCredential = await get_siigo_credentials()

    if siigoCredential is None:
        print("Error: Credential not found.")
        return None
    
    process_list = PROCESS

    if args.process:
        if args.process in process_list:
            process_list = [args.process]
        else:
            print(f"Error: Process {args.process} not found.")
            return None

    for process in process_list:
        await request_process(args, process, siigoCredential)

    await process_tasks()

    end_time = time.time()
    total_time = end_time - start_time
    print(f"Tiempo total de ejecución: {total_time:.2f} segundos")

if __name__ == '__main__':
    asyncio.run(main())