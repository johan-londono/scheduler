# Conexiones y credenciales del reenvío DIAN

Guía funcional del **cómo** y el **dónde** se obtienen las credenciales para localizar y reenviar los documentos electrónicos pendientes (facturas, notas crédito y documentos de soporte) de cada cliente a la DIAN.

---

## 1. Visión general

El reenvío a la DIAN **no se conecta a una sola base de datos**. Para procesar a todos los clientes en una sola corrida, el sistema trabaja en **tres capas** de credenciales:

| Capa | ¿Qué guarda? | ¿Dónde vive? | ¿Quién la usa? |
|------|--------------|--------------|----------------|
| **A. Credenciales globales** | Accesos a la DB maestra eSuite y a la API de emisión DIAN | Tabla `scheduler_credentials` del scheduler | La tarea Celery, al iniciar la ejecución |
| **B. Directorio de clientes** | Datos de conexión de la DB particular de cada cliente | Tabla `clientes_conexiones_db` en la DB maestra eSuite | El subprocess, para saber a quién consultar |
| **C. DB del cliente** | Los documentos DIAN realmente pendientes | DB propia de cada cliente (una por cliente) | El subprocess, para leer/actualizar los documentos |

La idea clave: **las credenciales globales son fijas; las credenciales de cada cliente se leen en caliente** del directorio, porque clientes se dan de alta o de baja y cambiar código por cada uno no es viable.

---

## 2. Capa A — Credenciales globales

### Dónde se configuran

En la tabla `scheduler_credentials` del scheduler. Cada fila es un "set" con un nombre (`name`) y un bloque de variables (`env_vars`) en formato JSON.

Ejemplo conceptual del set usado por DIAN:

```json
{
  "MAIN_DB_HOST":     "…",
  "MAIN_DB_NAME":     "esuite",
  "MAIN_DB_USER":     "…",
  "MAIN_DB_PASSWORD": "…",
  "MAIN_DB_PORT":     "5432",

  "API_PYTHON_URL":      "https://api.dian.proveedor/…",
  "API_PYTHON_USERNAME": "…",
  "API_PYTHON_PASSWORD": "…"
}
```

### Para qué sirven

- **MAIN_DB_\*** → acceso a la **DB maestra de eSuite**, donde vive el directorio de clientes (capa B).
- **API_PYTHON_\*** → acceso a la **API de emisión DIAN** (el proveedor de integración) al que se reenvían los documentos.

### Cómo se entregan al proceso

La tarea Celery las lee de `scheduler_credentials` y las "inyecta" como variables de entorno al subprocess que hace el trabajo pesado. Es decir, cuando la tarea corre, el subprocess ya encuentra estas credenciales disponibles sin tener que preguntarle nada a nadie.

### Cómo se modifican

Vía la API FastAPI del scheduler:

- `GET  /credentials` → lista (valores sensibles enmascarados)
- `PATCH /credentials/{id}` → actualiza/mezcla variables

Tras modificar, es necesario **reiniciar el worker y el beat** para que tomen los nuevos valores.

---

## 3. Capa B — Directorio de clientes

### Dónde vive

En la **DB maestra de eSuite** (a la que se accede con las credenciales `MAIN_DB_*` de la capa A), en la tabla `clientes_conexiones_db`.

### Qué contiene

Una fila por cliente con, entre otros campos:

| Columna | Significado |
|---------|-------------|
| `key_cli` | Clave corta del cliente (la que se usa en los kwargs de la tarea) |
| `nombre_cliente` | Nombre legible |
| `ip_db`, `puerto_db` | Host y puerto de la DB propia del cliente |
| `nombre_db` | Nombre de esa DB |
| `user_db`, `password_db` | Usuario y contraseña para conectarse a esa DB |
| `produccion` | Si ese cliente está en ambiente productivo |
| `transmitir` | **Interruptor maestro**: si está en `false`, el cliente se ignora |

### Cómo se usa

Al arrancar, el subprocess se conecta a la DB maestra y ejecuta:

- **Sin filtro de cliente** → `SELECT … WHERE transmitir = true` (todos los clientes habilitados).
- **Con `key_cli`/`key_clis` en los kwargs** → `SELECT … WHERE key_cli = 'xxx'` (sólo los indicados).

El resultado es una lista de clientes con sus credenciales de DB, que se recorre cliente por cliente.

### Implicaciones para procesos

- Alta de un cliente nuevo: basta con insertar su fila en `clientes_conexiones_db`. **No se modifica código ni scheduler**.
- Baja temporal: poner `transmitir = false`. El próximo ciclo lo ignora automáticamente.
- Cambio de password de la DB de un cliente: actualizar `password_db` en esa fila; el siguiente ciclo ya lo usa.

---

## 4. Capa C — DB de cada cliente

### Conexión

Por cada cliente retornado en la capa B, el subprocess abre una **conexión nueva y dedicada** a su DB usando exactamente los campos `ip_db`, `puerto_db`, `nombre_db`, `user_db`, `password_db` de la fila.

### Qué se consulta

Los documentos electrónicos pendientes de aceptación por la DIAN:

- **Facturas electrónicas (FE)**
- **Notas crédito electrónicas (NC)**
- **Documentos de soporte (DS)**

Por defecto se procesan solo los documentos del **mes en curso**. Esto puede ajustarse con filtros de fecha (día, mes, año o rango) vía los `env_vars` de la credencial.

### Qué se hace con ellos

1. Se agrupan y se envían a la **API de emisión DIAN** (credenciales `API_PYTHON_*` de la capa A).
2. Según la respuesta (aceptado / rechazado / error), se registra el resultado.
3. Al final, cada combinación (cliente, tipo de documento) emite un resumen estructurado que el scheduler captura.

---

## 5. Flujo completo de credenciales

```
┌──────────────────────────────────────────────────────────────────┐
│ 1. scheduler_credentials (DB scheduler)                          │
│    MAIN_DB_*  +  API_PYTHON_*                                    │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ inyecta como variables de entorno
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ 2. Subprocess reenvio_service                                    │
│    Con MAIN_DB_* se conecta a la DB maestra eSuite               │
└────────────────────────────────┬─────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ 3. clientes_conexiones_db  (directorio en DB maestra)            │
│    WHERE transmitir = true  [AND key_cli = 'xxx']                │
│    Devuelve: ip_db, nombre_db, user_db, password_db, …           │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ por cada cliente
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ 4. DB del cliente (una por cliente)                              │
│    SELECT documentos DIAN pendientes (mes actual)                │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ con API_PYTHON_*
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ 5. API de emisión DIAN                                           │
│    Recibe y responde por cada documento reenviado                │
└──────────────────────────────────────────────────────────────────┘
```

---

## 6. Responsabilidades por área

| Cambio requerido | Dónde actuar | Reinicio de servicios |
|------------------|--------------|----------------------|
| Cambiar contraseña de la DB maestra eSuite | `scheduler_credentials` (set DIAN) → `MAIN_DB_PASSWORD` | Sí |
| Cambiar credenciales de la API DIAN | `scheduler_credentials` (set DIAN) → `API_PYTHON_*` | Sí |
| Alta de un cliente nuevo | Insertar fila en `clientes_conexiones_db` | No |
| Suspender temporalmente un cliente | `transmitir = false` en su fila | No |
| Cambiar credenciales de la DB de un cliente | Actualizar su fila en `clientes_conexiones_db` | No |
| Cambiar horario del reenvío | `scheduler_tasks` (cron de la tarea) vía API | Sí |
| Cambiar clientes procesados en una tarea | `kwargs` de la tarea (`key_cli`/`key_clis`) vía API | Sí |
| Cambiar rango de fechas procesado | `env_vars` de la credencial (`FILTRO_MES`, etc.) | Sí |

---

## 7. Seguridad y buenas prácticas

- **Separación de credenciales**: las credenciales sensibles (DB maestra, API DIAN) no viven en código ni en archivos sueltos, sino en `scheduler_credentials`. Los endpoints de listado las entregan enmascaradas.
- **Credenciales por cliente aisladas**: cada cliente tiene su propio usuario/contraseña de DB; comprometer una no expone a las demás.
- **Interruptor `transmitir`**: permite excluir a un cliente de toda automatización sin borrar su registro, útil para mantenimientos o migraciones.
- **Filtrado temporal por defecto**: el procesamiento del mes en curso limita el volumen y evita reenvíos masivos accidentales sobre histórico.
- **Reporte consolidado**: todas las ejecuciones del día se agrupan y se envían en un único correo una vez al día, con desglose por tipo de documento y por cliente, incluyendo errores de conexión (clientes a los que no se pudo acceder) para seguimiento.

---

## 8. Preguntas frecuentes

**¿Por qué hay dos bases de datos involucradas?**
Porque eSuite es multi-cliente. La DB maestra actúa como "guía telefónica" de los clientes; cada cliente tiene su propia DB operativa. Centralizar todo en una sola DB no es viable por razones de aislamiento y performance.

**¿Qué pasa si la DB de un cliente está caída o sin permisos?**
El subprocess captura el error de conexión, lo registra como "error de cx" para ese cliente y continúa con el siguiente. El correo consolidado del día lista todos los clientes con problemas de acceso.

**¿Cómo se sabe qué documentos están pendientes?**
Cada DB de cliente guarda el estado DIAN de sus documentos. El reenvío busca los que **no** aparecen como aceptados y los reintenta a través de la API.

**¿Quién autoriza un cambio de credenciales?**
Las credenciales globales (capa A) las gestiona el equipo técnico del scheduler. Las credenciales por cliente (capa B) se actualizan como parte del alta/soporte del cliente en la DB maestra eSuite.
