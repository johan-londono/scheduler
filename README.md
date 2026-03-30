# Scheduler - Sistema de Tareas Programadas

Sistema de tareas programadas con Python, Celery y Redis. Permite ejecutar funciones internas en horarios y días específicos usando colas de Redis.

## Stack

- **Python 3.10+**
- **Celery** — cola de tareas distribuida
- **Celery Beat** — programador de tareas con soporte cron
- **Redis** — broker de mensajes y backend de resultados
- **YAML** — configuración de tareas y horarios

## Estructura del proyecto

```
scheduler/
├── config/
│   └── tasks.yaml        # Definición de tareas y horarios
├── tasks/
│   ├── __init__.py
│   └── scripts.py         # Funciones ejecutables como tareas
├── app.py                 # Configuración de Celery y carga dinámica
├── requirements.txt       # Dependencias
└── README.md
```

## Requisitos previos

- Python 3.10 o superior
- Redis corriendo en `localhost:6379` (o configurar `REDIS_URL`)

### Instalar Redis (si no está instalado)

En Ubuntu/Debian:
```bash
sudo apt-get update
sudo apt-get install -y redis-server
```

Verificar que Redis esté corriendo:
```bash
redis-cli ping
# Debe responder: PONG
```

## Instalación

### 0. Instalar por completo

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt 
```

### 1. Crear entorno virtual

```bash
cd /home/gojofx/projects/eholding/scheduler
python3 -m venv .venv
```

### 2. Activar el entorno virtual

```bash
source .venv/bin/activate
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno

Crear un archivo `.env` en la raíz del proyecto con las siguientes variables:

```bash
# Redis
REDIS_URL=redis://localhost:6379/0

# Configuración de correo
MAIL_MAILER=smtp
MAIL_HOST=smtp.googlemail.com
MAIL_PORT=587
MAIL_USERNAME="tu_correo@dominio.com"
MAIL_PASSWORD="tu_contraseña_de_aplicación"
MAIL_FROM_ADDRESS="tu_correo@dominio.com"
MAIL_TO="destinatario@dominio.com"
MAIL_ENCRYPTION=tls
```

**Nota sobre contraseñas de Gmail:** Para usar Gmail, debes generar una "Contraseña de aplicación" desde tu cuenta de Google (Seguridad → Verificación en dos pasos → Contraseñas de aplicaciones).

**Múltiples destinatarios:** Puedes especificar varios destinatarios separados por comas:
```bash
MAIL_TO="correo1@dominio.com,correo2@dominio.com,correo3@dominio.com"
```

## Configuración de tareas

Las tareas se definen en `config/tasks.yaml`. Cada tarea tiene los siguientes campos:

| Campo      | Descripción                                        | Requerido |
|------------|----------------------------------------------------|-----------|
| `name`     | Identificador único de la tarea                    | Sí        |
| `function` | Ruta completa de la función (`modulo.funcion`)     | Sí        |
| `schedule` | Programación tipo cron (ver tabla abajo)           | Sí        |
| `args`     | Lista de argumentos posicionales                   | No        |
| `kwargs`   | Diccionario de argumentos con nombre               | No        |

### Campos del schedule

| Campo            | Valores posibles                              | Default |
|------------------|-----------------------------------------------|---------|
| `minute`         | `0-59`, `*`, o listas como `0,30`             | `*`     |
| `hour`           | `0-23`, `*`, o listas como `8,20`             | `*`     |
| `day_of_week`    | `mon,tue,wed,thu,fri,sat,sun` o `*`           | `*`     |
| `day_of_month`   | `1-31`, `*`                                   | `*`     |
| `month_of_year`  | `1-12`, `*`                                   | `*`     |

### Ejemplo de configuración

```yaml
tasks:
  - name: "reporte_diario"
    function: "tasks.scripts.generar_reporte"
    schedule:
      minute: "0"
      hour: "8"
      day_of_week: "mon,tue,wed,thu,fri"
    args: []
    kwargs:
      tipo: "diario"

  - name: "limpieza_semanal"
    function: "tasks.scripts.limpiar_datos"
    schedule:
      minute: "30"
      hour: "2"
      day_of_week: "sun"

  - name: "respaldo_nocturno"
    function: "tasks.scripts.crear_respaldo"
    schedule:
      minute: "0"
      hour: "3"
      day_of_week: "*"
    kwargs:
      destino: "/tmp/respaldos"
```

## Crear una nueva tarea

### 1. Escribir la función

En `tasks/scripts.py`, agregar la función decorada con `@celery_app.task`:

```python
from app import celery_app

@celery_app.task(name="tasks.scripts.mi_tarea")
def mi_tarea(parametro="valor_default"):
    # Lógica de la tarea
    return f"Tarea completada con {parametro}"
```

El atributo `name` del decorador debe coincidir con el valor de `function` en el YAML.

### 2. Registrar en el YAML

Agregar la entrada en `config/tasks.yaml`:

```yaml
  - name: "mi_tarea"
    function: "tasks.scripts.mi_tarea"
    schedule:
      minute: "*/15"
      hour: "*"
    kwargs:
      parametro: "mi_valor"
```

### 3. Reiniciar los procesos

Después de agregar o modificar tareas, reiniciar el worker y beat para que tomen los cambios.

## Ejecución

### Desarrollo (worker + beat en un solo proceso)

Este es el modo recomendado para desarrollo y pruebas. Ejecuta el worker y el scheduler en un solo proceso.

#### Opción 1: Con entorno virtual activado

```bash
cd /home/gojofx/projects/eholding/scheduler
source .venv/bin/activate
set -a && source .env && set +a
celery -A app worker --beat --loglevel=info
```

#### Opción 2: Sin activar el entorno (comando directo)

```bash
cd /home/gojofx/projects/eholding/scheduler
set -a && source .env && set +a
.venv/bin/python -m celery -A app worker --beat --loglevel=info
```

**Explicación de comandos:**
- `set -a && source .env && set +a`: Carga todas las variables del archivo `.env` al entorno actual
- `-A app`: Especifica el módulo de la aplicación Celery
- `--beat`: Inicia también el scheduler (beat) para ejecutar tareas programadas
- `--loglevel=info`: Muestra logs informativos en la consola

**Detener el servicio:**
Presiona `Ctrl + C` en la terminal donde está corriendo.

### Producción (procesos separados)

En entornos de producción se recomienda separar el worker (ejecutor) del beat (programador) para mayor estabilidad y escalabilidad.

**Terminal 1 — Worker (ejecuta las tareas):**
```bash
cd /home/gojofx/projects/eholding/scheduler
source .venv/bin/activate
set -a && source .env && set +a
celery -A app worker --loglevel=info
```

**Terminal 2 — Beat (programa las tareas):**
```bash
cd /home/gojofx/projects/eholding/scheduler
source .venv/bin/activate
set -a && source .env && set +a
celery -A app beat --loglevel=info
```

### Ejecutar en background

Para dejar el servicio corriendo en segundo plano:

```bash
cd /home/gojofx/projects/eholding/scheduler
set -a && source .env && set +a
nohup .venv/bin/python -m celery -A app worker --beat --loglevel=info > celery.log 2>&1 &
```

**Detener servicio en background:**
```bash
pkill -f "celery -A app worker"
```

**Ver logs en tiempo real:**
```bash
tail -f celery.log
```

### Múltiples workers

Para mayor concurrencia se pueden levantar varios workers:

```bash
celery -A app worker --loglevel=info --concurrency=4
```

## Variables de entorno

### Redis

| Variable    | Descripción                    | Default                      |
|-------------|--------------------------------|------------------------------|
| `REDIS_URL` | URL de conexión a Redis        | `redis://localhost:6379/0`   |

### Correo electrónico

| Variable           | Descripción                                      | Requerido | Ejemplo                        |
|--------------------|--------------------------------------------------|-----------|--------------------------------|
| `MAIL_MAILER`      | Tipo de mailer (siempre `smtp`)                  | Sí        | `smtp`                         |
| `MAIL_HOST`        | Servidor SMTP                                    | Sí        | `smtp.googlemail.com`          |
| `MAIL_PORT`        | Puerto del servidor SMTP                         | Sí        | `587`                          |
| `MAIL_USERNAME`    | Usuario para autenticación SMTP                  | Sí        | `usuario@gmail.com`            |
| `MAIL_PASSWORD`    | Contraseña o contraseña de aplicación            | Sí        | `abcd efgh ijkl mnop`          |
| `MAIL_FROM_ADDRESS`| Dirección del remitente                          | Sí        | `remitente@dominio.com`        |
| `MAIL_TO`          | Destinatarios (separados por coma)               | Sí        | `dest1@mail.com,dest2@mail.com`|
| `MAIL_ENCRYPTION`  | Tipo de cifrado (`tls`, `ssl` o vacío)           | No        | `tls`                          |

### Servidores SMTP comunes

**Gmail:**
```bash
MAIL_HOST=smtp.googlemail.com
MAIL_PORT=587
MAIL_ENCRYPTION=tls
```
*Nota: Requiere "Contraseña de aplicación" si tienes verificación en dos pasos.*

**Outlook/Hotmail:**
```bash
MAIL_HOST=smtp-mail.outlook.com
MAIL_PORT=587
MAIL_ENCRYPTION=tls
```

**Office 365:**
```bash
MAIL_HOST=smtp.office365.com
MAIL_PORT=587
MAIL_ENCRYPTION=tls
```

### Ejemplo de archivo .env completo

```bash
# Redis
REDIS_URL=redis://localhost:6379/0

# Correo
MAIL_MAILER=smtp
MAIL_HOST=smtp.googlemail.com
MAIL_PORT=587
MAIL_USERNAME="mi.correo@gmail.com"
MAIL_PASSWORD="abcd efgh ijkl mnop"
MAIL_FROM_ADDRESS="mi.correo@gmail.com"
MAIL_TO="destinatario1@ejemplo.com,destinatario2@ejemplo.com"
MAIL_ENCRYPTION=tls
```

## Ejemplos de schedules comunes

| Descripción                        | Configuración                                                  |
|------------------------------------|----------------------------------------------------------------|
| Cada minuto                        | `minute: "*"`                                                  |
| Cada 15 minutos                    | `minute: "*/15"`                                               |
| Cada hora en punto                 | `minute: "0"`                                                  |
| Lunes a viernes a las 8:00        | `minute: "0"`, `hour: "8"`, `day_of_week: "mon,tue,wed,thu,fri"` |
| Domingos a las 2:30 AM            | `minute: "30"`, `hour: "2"`, `day_of_week: "sun"`             |
| Primer día del mes a medianoche   | `minute: "0"`, `hour: "0"`, `day_of_month: "1"`               |
| Cada día a las 3:00 AM            | `minute: "0"`, `hour: "3"`                                    |

## Monitoreo y diagnóstico

### Ver tareas activas (en ejecución)

```bash
cd /home/gojofx/projects/eholding/scheduler
source .venv/bin/activate
celery -A app inspect active
```

### Ver tareas registradas

```bash
celery -A app inspect registered
```

Esto te mostrará todas las tareas que Celery puede ejecutar. Deberías ver algo como:
```
-> celery@hostname: OK
    * tasks.scripts.crear_respaldo
    * tasks.scripts.enviar_correo
    * tasks.scripts.generar_reporte
    * tasks.scripts.limpiar_datos
```

### Ver estadísticas del worker

```bash
celery -A app inspect stats
```

### Ejecutar una tarea manualmente

Para probar una tarea sin esperar su horario programado:

```bash
cd /home/gojofx/projects/eholding/scheduler
source .venv/bin/activate
set -a && source .env && set +a

# Ejecutar tarea de correo
celery -A app call tasks.scripts.enviar_correo

# Ejecutar con argumentos personalizados
celery -A app call tasks.scripts.enviar_correo --kwargs='{"asunto":"Prueba manual","mensaje":"Correo de prueba"}'

# Ejecutar reporte
celery -A app call tasks.scripts.generar_reporte --kwargs='{"tipo":"semanal"}'
```

### Ver cola de tareas pendientes

```bash
celery -A app inspect scheduled
```

### Purgar todas las tareas pendientes

**Advertencia:** Esto eliminará todas las tareas en cola.
```bash
celery -A app purge
```

### Ver workers disponibles

```bash
celery -A app inspect ping
```

## Zona horaria

El sistema está configurado con la zona horaria `America/Mexico_City`. Para cambiarla, modifica el valor de `timezone` en `app.py`:

```python
celery_app.conf.update(
    timezone="America/Mexico_City",  # Cambiar aquí
    ...
)
```

## Solución de problemas

### Error: "Received unregistered task"

**Síntoma:** Celery reporta que no puede encontrar la tarea.

**Soluciones:**
1. Verifica que el decorador `@celery_app.task(name="...")` tenga el mismo nombre que en `config/tasks.yaml`
2. Reinicia el worker después de agregar nuevas tareas
3. Verifica que el módulo `tasks/scripts.py` esté correctamente importado

### Error: "Connection refused" al conectar con Redis

**Síntoma:** No puede conectarse a Redis.

**Soluciones:**
1. Verifica que Redis esté corriendo: `redis-cli ping` (debe responder `PONG`)
2. Si no está instalado: `sudo apt-get install redis-server`
3. Inicia Redis: `sudo systemctl start redis-server`
4. Verifica el `REDIS_URL` en tu `.env`

### Error: "SMTPAuthenticationError"

**Síntoma:** Fallo de autenticación al enviar correo.

**Soluciones:**
1. Para Gmail: Usa una "Contraseña de aplicación", no tu contraseña normal
2. Verifica que `MAIL_USERNAME` y `MAIL_PASSWORD` estén correctos
3. Confirma que el acceso a aplicaciones menos seguras esté habilitado (si aplica)

### Los correos no se envían

**Diagnóstico:**
1. Verifica que el worker esté corriendo: `celery -A app inspect active`
2. Revisa los logs para ver errores específicos
3. Prueba enviar un correo manualmente:
   ```bash
   celery -A app call tasks.scripts.enviar_correo --kwargs='{"asunto":"Test","mensaje":"Prueba"}'
   ```
4. Verifica que `MAIL_TO` esté configurado en `.env`

### Las tareas no se ejecutan en el horario programado

**Diagnóstico:**
1. Confirma que el servicio beat esté corriendo (verifica en logs: `[Beat]`)
2. Revisa la configuración del schedule en `config/tasks.yaml`
3. Verifica la zona horaria en `app.py`
4. Reinicia worker y beat después de cambios en `tasks.yaml`

## Mejores prácticas

1. **Usa `.env` para secretos:** Nunca hagas commit de `.env` con credenciales reales
2. **Monitorea logs:** Revisa regularmente los logs para detectar problemas
3. **Prueba tareas manualmente:** Antes de programar, ejecuta tareas con `celery call`
4. **Reinicia después de cambios:** Siempre reinicia worker/beat tras modificar código o YAML
5. **Usa entorno virtual:** Mantén dependencias aisladas con `.venv`
6. **Contraseñas de aplicación:** Para Gmail, usa contraseñas de aplicación, no tu contraseña principal
