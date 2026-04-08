# Reenvío automático de documentos a la DIAN

Módulo Celery para reenviar facturas electrónicas, notas crédito y documentos de soporte pendientes de aceptación por la DIAN.

---

## Arquitectura

```
Celery Beat
    ↓  encola según cron (varias veces al día)
reenviar_documentos_dian (Celery task)
    ↓  subprocess
esuite_dian_app_v2/reenvio_service/main.py --tipo <X>
    ↓  asyncpg (conexión directa a DB de cada cliente)
DIAN (API de integración)
    ↓  stdout → RESUMEN_JSON
_acumular_resultado_dian
    ↓  RPUSH
Redis  →  clave: dian:reporte:YYYY-MM-DD  (TTL 48h)
    ↓  a las 17:00
enviar_reporte_dian_diario (Celery task)
    ↓
enviar_correo (un solo correo consolidado del día)
```

Cada ejecución de reenvío **acumula** su resultado en Redis.  
El correo se envía **una sola vez al día a las 17:00** con la información consolidada de todas las ejecuciones y tipos de documento.

---

## Tipos de documento soportados

| `tipo_doc` (kwarg DB) | `--tipo` al CLI | Documentos procesados            |
|-----------------------|-----------------|----------------------------------|
| `"facturas"`          | `facturas`      | Facturas electrónicas (FE)       |
| `"notas_credito"`     | `nc`            | Notas crédito electrónicas (NC)  |
| `"documentos_soporte"`| `docsoporte`    | Documentos de soporte (DS)       |

---

## Tareas registradas

### `reenviar_documentos_dian`

Ejecuta el reenvío para el tipo de documento indicado y acumula el resultado en Redis. **No envía correo.**

| kwarg        | Tipo    | Requerido | Descripción                                                          |
|--------------|---------|-----------|----------------------------------------------------------------------|
| `tipo_doc`   | `str`   | Sí        | `"facturas"`, `"notas_credito"` o `"documentos_soporte"`            |
| `key_cli`    | `str`   | No        | Procesar solo este cliente. Vacío = todos                            |
| `env_config` | `dict`  | No        | Variables de entorno para el subprocess (viene de `credentials_id`) |

---

### `enviar_reporte_dian_diario`

Lee Redis, consolida todas las ejecuciones del día y envía el correo. **Programar a las 17:00.**

| kwarg          | Tipo        | Requerido | Descripción                                              |
|----------------|-------------|-----------|----------------------------------------------------------|
| `destinatarios`| `list[str]` | No        | Correos destino. Vacío = usa `MAIL_TO` del entorno       |

---

## Configuración en la DB

### Facturas electrónicas (FE)

```json
{
  "name":           "reenvio_facturas_dian",
  "function":       "tasks.reenvio_dian.reenviar_documentos_dian",
  "kwargs":         { "tipo_doc": "facturas" },
  "hour":           "6",
  "minute":         "0",
  "credentials_id": 1
}
```

### Notas crédito (NC)

```json
{
  "name":           "reenvio_nc_dian",
  "function":       "tasks.reenvio_dian.reenviar_documentos_dian",
  "kwargs":         { "tipo_doc": "notas_credito" },
  "hour":           "6",
  "minute":         "15",
  "credentials_id": 1
}
```

### Documentos de soporte (DS)

```json
{
  "name":           "reenvio_docsoporte_dian",
  "function":       "tasks.reenvio_dian.reenviar_documentos_dian",
  "kwargs":         { "tipo_doc": "documentos_soporte" },
  "hour":           "6",
  "minute":         "30",
  "credentials_id": 1
}
```

### Reporte diario (17:00) — obligatorio

```json
{
  "name":     "reporte_dian_diario",
  "function": "tasks.reenvio_dian.enviar_reporte_dian_diario",
  "kwargs":   {},
  "hour":     "17",
  "minute":   "0"
}
```

Con destinatarios explícitos:

```json
{
  "name":     "reporte_dian_diario",
  "function": "tasks.reenvio_dian.enviar_reporte_dian_diario",
  "kwargs":   { "destinatarios": ["contabilidad@empresa.com", "ops@empresa.com"] },
  "hour":     "17",
  "minute":   "0"
}
```

### Con `key_cli` (un cliente específico)

```json
{
  "name":           "reenvio_facturas_cliente_abc",
  "function":       "tasks.reenvio_dian.reenviar_documentos_dian",
  "kwargs":         { "tipo_doc": "facturas", "key_cli": "CLI001" },
  "hour":           "7",
  "minute":         "0",
  "credentials_id": 1
}
```

---

## Crear tareas vía API

```bash
# Facturas
POST http://localhost:8080/tasks
{ "name": "reenvio_facturas_dian", "function": "tasks.reenvio_dian.reenviar_documentos_dian",
  "kwargs": {"tipo_doc": "facturas"}, "hour": "6", "minute": "0", "credentials_id": 1 }

# Notas crédito
POST http://localhost:8080/tasks
{ "name": "reenvio_nc_dian", "function": "tasks.reenvio_dian.reenviar_documentos_dian",
  "kwargs": {"tipo_doc": "notas_credito"}, "hour": "6", "minute": "15", "credentials_id": 1 }

# Documentos de soporte
POST http://localhost:8080/tasks
{ "name": "reenvio_docsoporte_dian", "function": "tasks.reenvio_dian.reenviar_documentos_dian",
  "kwargs": {"tipo_doc": "documentos_soporte"}, "hour": "6", "minute": "30", "credentials_id": 1 }

# Reporte diario (17:00)
POST http://localhost:8080/tasks
{ "name": "reporte_dian_diario", "function": "tasks.reenvio_dian.enviar_reporte_dian_diario",
  "kwargs": {}, "hour": "17", "minute": "0" }
```

Después de crear las tareas, reiniciar los servicios:

```bash
# Vía API
POST http://localhost:8080/system/restart

# Vía CLI
bash scripts/reiniciar.sh
```

---

## Ejecución inmediata (manual)

```bash
# Vía API
POST http://localhost:8080/tasks/reenvio_facturas_dian/run
POST http://localhost:8080/tasks/reenvio_nc_dian/run
POST http://localhost:8080/tasks/reporte_dian_diario/run

# Vía CLI Celery
celery -A app call tasks.reenvio_dian.reenviar_documentos_dian \
  --kwargs '{"tipo_doc": "facturas"}'

celery -A app call tasks.reenvio_dian.enviar_reporte_dian_diario
```

---

## Correo de notificación

El correo se envía **una vez al día a las 17:00** con la información consolidada de todas las ejecuciones del día.

**Asunto:** `Reporte DIAN — DD/MM/YYYY — Sin errores / Con errores`

**Contenido:**
- Totales por tipo de documento (FE / NC / DS): exitosos, fallidos, errores de conexión
- Detalle de documentos fallidos con código y razón
- Detalle de errores de conexión por cliente
- Errores de ejecución del subprocess (si los hubo)

**Condición de envío:**

| Situación del día                            | ¿Se envía correo? |
|----------------------------------------------|:-----------------:|
| Sin ejecuciones registradas en Redis         | No                |
| Todas las ejecuciones sin documentos         | Sí (sin errores)  |
| Al menos un documento exitoso                | Sí                |
| Al menos un documento fallido                | Sí (con errores)  |
| Al menos un error de conexión                | Sí (con errores)  |
| Error de ejecución (returncode ≠ 0)          | Sí (con errores)  |

---

## Almacenamiento en Redis

Cada ejecución escribe una entrada en la lista Redis:

```
Clave:  dian:reporte:YYYY-MM-DD
Tipo:   Lista (RPUSH)
TTL:    48 horas
```

Estructura de cada entrada:

```json
{
  "tipo_doc":   "facturas",
  "etiqueta":   "facturas",
  "exito":      true,
  "returncode": 0,
  "resumen": {
    "clientes": 2, "facturas": 5, "exitosas": 4, "fallidas": 1,
    "errores_cx": 0, "fallidas_detalle": [...], "errores_conexion": [...]
  },
  "stderr":     "",
  "timestamp":  "2026-04-06T08:00:12.345678-06:00"
}
```

Al enviar el reporte, la clave se elimina de Redis.

---

## Protocolo RESUMEN_JSON

El script `reenvio_service.main` imprime en `stdout` la siguiente línea al finalizar:

```
RESUMEN_JSON:{"clientes":2,"facturas":5,"exitosas":4,"fallidas":1,"errores_cx":0,"fallidas_detalle":[...],"errores_conexion":[...],"nombres_clientes":["ABC","XYZ"]}
```

| Campo              | Tipo         | Descripción                                                              |
|--------------------|--------------|--------------------------------------------------------------------------|
| `clientes`         | `int`        | Total de clientes procesados                                             |
| `facturas`         | `int`        | Total de documentos procesados (nombre genérico para todos los tipos)    |
| `exitosas`         | `int`        | Documentos aceptados por la DIAN                                         |
| `fallidas`         | `int`        | Documentos rechazados o con error                                        |
| `errores_cx`       | `int`        | Clientes con error de conexión a su DB                                   |
| `fallidas_detalle` | `list[dict]` | Detalle de cada documento fallido                                        |
| `errores_conexion` | `list[dict]` | Detalle de cada error de conexión (`key_cli`, `cliente`, `error`)        |
| `nombres_clientes` | `list[str]`  | Nombres de los clientes procesados                                       |

---

## Alias de compatibilidad

Las tareas antiguas siguen registradas para no romper entradas existentes en la DB.  
El parámetro `destinatarios` que tenían se ignora; el correo ahora lo gestiona `enviar_reporte_dian_diario`.

| Nombre registrado                                     | Equivale a                                                 |
|-------------------------------------------------------|------------------------------------------------------------|
| `tasks.reenvio_dian.reenviar_facturas_dian`           | `reenviar_documentos_dian(tipo_doc="facturas")`            |
| `tasks.reenvio_dian.reenviar_documentos_soporte_dian` | `reenviar_documentos_dian(tipo_doc="documentos_soporte")`  |

---

## Agregar un nuevo tipo de documento

1. Agregar en `_TIPOS_CLI_DIAN` dentro de `tasks/reenvio_dian.py`:
   ```python
   "nuevo_tipo": "nuevo_tipo_cli",
   ```
2. Agregar en `_ETIQUETAS_DIAN`:
   ```python
   "nuevo_tipo": "nombre para correo y logs",
   ```
3. Agregar el valor en `TIPOS_VALIDOS` de `esuite_dian_app_v2/reenvio_service/main.py` e implementar la lógica de procesamiento.
4. Crear la tarea en la DB vía `POST /tasks` con `"tipo_doc": "nuevo_tipo"` y reiniciar.
