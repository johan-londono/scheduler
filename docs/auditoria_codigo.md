# Auditoría del scheduler — 2026-08-09

Revisión completa de código, configuración en base de datos y documentación.
Rama `siigo-sync-refactor`, commit base `08c411d`.

> **Estado: corregido en código el 2026-08-09.** Ver la sección
> [Estado de las correcciones](#estado-de-las-correcciones) al final: todo lo que
> se arregla en código está hecho y con checks; queda pendiente lo que solo se
> puede cambiar en la base de datos o que requiere una decisión de negocio.

Alcance: `app.py`, `beat_scheduler.py`, `db.py`, `api/`, `tasks/`, `scripts/`, `systemd/`,
`test/`, README, CLAUDE.md, docs/ y el contenido real de `scheduler_tasks` /
`scheduler_credentials` en producción.

> **Nota sobre rutas.** Las referencias a `scripts/…` de este informe son las del
> momento de la auditoría. Al reorganizarse el proyecto, `scripts/` se dividió en
> `etl/` (carga de datos), `reenvio/` (reenvío a la DIAN) y `ops/` (operación).
> La correspondencia está en la tabla M6 del estado de correcciones.

---

## Resumen ejecutivo

| Categoría | Hallazgos | Críticos |
|-----------|-----------|----------|
| Falsos positivos / reportes erróneos | 10 | 4 |
| Bugs de correctitud y seguridad | 24 | 3 |
| Inconsistencias de documentación | 10 | 2 |
| Código muerto / dependencias sin uso | 5 | — |

**Los cinco problemas que hay que atender primero:**

1. **FP-1** — El "reporte DIAN diario" está programado con `day_of_month = 1`: se envía
   una vez al mes, no una vez al día. Los otros 29 días de datos expiran en Redis (TTL 48h)
   sin que nadie los lea. Es un cambio de una fila en la DB.
2. **FP-2** — `reenvio_service/main.py` sale con **código 0** en sus cuatro fallos fatales
   (token, DB maestra, cliente inexistente, sin clientes). La tarea Celery interpreta
   `returncode == 0` como éxito y acumula un resumen a ceros → el correo consolidado
   informa **"Sin errores"** cuando en realidad no se procesó nada.
3. **B-3** — `JWT_SECRET_KEY` tiene default `""`. PyJWT firma HS256 con clave vacía sin
   protestar (verificado en este entorno). Si la variable falta en el `.env` del servidor,
   cualquiera puede forjar un token de admin.
4. **FP-3** — En la sincronización Siigo solo se evalúa la página 1; el resultado de las
   páginas 2..N se descarta. Si todas fallan, el correo reporta `OK` y además presume
   "N página(s) adicional(es)".
5. **B-4** — Hay tareas en la DB cuyos `kwargs` no encajan con la firma de la función
   (`plantilla` en Dominus, falta `customer_id` en Siigo mes anterior). Revientan con
   `TypeError` en cuanto se activen; la API las aceptó sin validar nada.

---

## 1. Falsos positivos y reportes erróneos

Esta sección responde directamente a la preocupación de "respuestas erróneas o falsos
positivos". Ordenada por impacto.

### FP-1 — El reporte DIAN "diario" solo se envía el día 1 de cada mes · CRÍTICO

`scheduler_tasks` id 10:

```
name        = reporte_dian_diario
function    = tasks.reenvio_dian.enviar_reporte_dian_diario
minute=0  hour=18  day_of_month=1   activa=true
```

`day_of_month = 1` convierte el cron en mensual. Consecuencias encadenadas:

- La clave Redis es `dian:reporte:YYYY-MM-DD` con **TTL de 48 h**
  (`tasks/reenvio_dian.py:105`). Los resultados de los días 2 a 31 se borran solos.
- Cuando el reporte por fin corre, lee `dian:reporte:<hoy>`, es decir **solo las
  ejecuciones del día 1**. El resto del mes nunca se reporta a nadie.
- Al final el código hace `DELETE` de la clave (`tasks/reenvio_dian.py:485`), de modo que
  incluso ese día queda sin rastro.

Además el docstring del módulo (`tasks/reenvio_dian.py:16`) y `docs/reenvio_dian.md` dicen
**17:00**; la tarea real está a las **18:00**.

**Corrección:** `PATCH /tasks/reporte_dian_diario {"day_of_month": "*"}` y alinear la hora
con la documentación.

### FP-2 — El subprocess DIAN sale con código 0 cuando falla de raíz · CRÍTICO

`scripts/reenvio_service/main.py` hace `return` (no `sys.exit(1)`) en:

| Línea | Situación |
|-------|-----------|
| 73 | No se pudo obtener el token de la API DIAN |
| 82 | No se pudo conectar a la DB maestra (`MAIN_DB_*`) |
| 90 | No se pudo conectar a la DB del scheduler |
| 99 | `--key-cli` no existe en `clientes_conexiones_db` |
| 109 | No hay clientes con `transmitir = true` |

En los cinco casos el proceso termina con **exit code 0** y **sin imprimir la línea
`RESUMEN_JSON:`**. Del lado de Celery:

- `tasks/reenvio_dian.py:312` → `exito = resultado.returncode == 0` → `True`.
- `_parsear_resumen` (`:149`) no encuentra la línea → devuelve el fallback **a ceros**.
- El reporte consolidado suma ceros, `hay_errores` es `False` → asunto
  **"Reporte DIAN — dd/mm/aaaa — Sin errores"**.

Es decir: la caída total del servicio se comunica como una jornada limpia. Es el peor
falso positivo del sistema.

Efecto secundario en la línea 99: se retorna sin cerrar `scheduler_pool` (fuga de
conexiones asyncpg hasta que muere el proceso).

**Corrección:** `sys.exit(1)` en esos cinco puntos, e imprimir siempre `RESUMEN_JSON` —
aunque sea con un campo `error`.

### FP-3 — Siigo: solo se verifica la primera página · ALTO

`scripts/sync_siigo.py:311-339`. Los workers de la cola llaman `_request_api` y **descartan
el retorno**:

```python
try:
    await self._request_api(http_client, *task)   # ← el None de un fallo se pierde
except Exception as e:
    logger.error(...)
```

`run_sync` construye `ok`/`failed` únicamente con el resultado de `_seed_process`
(página 1, línea 388). Después, `tasks/sincronizar_cliente_siigo.py:69-77`:

```python
ok = proceso in data["ok"]
"detalle": f"{data['queued']} pagina(s) adicional(es) - {data['elapsed']:.1f}s"
```

Si la página 1 responde y las 200 siguientes fallan (token expirado a mitad de corrida,
rate limit, 500 del proveedor), el correo reporta **OK** y presume el número de páginas
que *encoló*, no las que *tuvo éxito*. `queued` es el tamaño de la cola, nunca un conteo
de páginas confirmadas.

**Corrección:** que los workers devuelvan resultado, contar fallos por proceso y degradar
el estado a `PARCIAL`/`ERROR`.

### FP-4 — `ALREADY_EMITTED` cuenta como éxito en NC y DS, pero como fallo en facturas · ALTO

| Archivo | Línea | Condición de éxito |
|---------|-------|--------------------|
| `scripts/reenvio_service/reenvio.py` | 181 | `if succeeded:` |
| `scripts/reenvio_service/reenvio_nc.py` | 187 | `if succeeded or reason_code == 'ALREADY_EMITTED':` |
| `scripts/reenvio_service/reenvio_docsoporte.py` | 158 | `if succeeded or reason_code == 'ALREADY_EMITTED':` |

Un documento que la DIAN ya tenía emitido se contabiliza como **envío exitoso nuevo** en
NC y DS (inflando "exitosas") y como **fallo** en facturas (inflando "fallidas" y
generando una fila de error en `dianenvio_errores`). Las tres cifras del correo consolidado
no son comparables entre sí.

Hay una segunda asimetría en el mismo bloque: `fallidas_detalle` **solo existe en
facturas** (`reenvio.py:208`). El `_summary` de NC y DS ni siquiera acepta el parámetro,
así que el desglose "Detalle de … fallidos" del correo (`tasks/reenvio_dian.py:420`)
**nunca muestra notas crédito ni documentos de soporte**, por más que fallen.

### FP-5 — El monitor de APIs considera sana cualquier respuesta < 500 · ALTO

`tasks/monitor_estado_apis.py:22`:

```python
"estado": "OK" if resp.status_code < 500 else "ERROR",
```

Un `404`, un `401`, un `403` o un `301` a una página de error se reportan como **OK**.
Como además se hace `GET` a la raíz del servicio (`http://…:8000`), basta con que el puerto
conteste *cualquier cosa* para que el monitor dé luz verde. El monitor puede estar tapando
un servicio degradado durante días.

**Corrección:** apuntar a un endpoint de health real y exigir `2xx` (o `resp.ok`).

### FP-6 — Documentos agotados por MAX_INTENTOS desaparecen en silencio · MEDIO

Tres rutas distintas al mismo punto ciego:

- **Facturas y NC**: el filtro está en el SQL
  (`COALESCE(diannumeroenvios,0) < $1`, `reenvio.py:42`, `reenvio_nc.py:42`). Al tercer
  intento el documento deja de aparecer en la consulta y jamás vuelve a mencionarse.
- **Doc. soporte**: se cuenta con `get_intentos_docsoporte` y se marca `omitidas`
  (`reenvio_docsoporte.py:136`).
- En el correo, el estado de la fila por cliente se decide **solo** con
  `tiene_fallo = r["fallidas"] > 0` (`tasks/reenvio_dian.py:408`). Un cliente con
  0 exitosas y 12 omitidas sale como **OK — "12 omitida(s)"**.

Documentos que la DIAN nunca aceptó quedan permanentemente fuera del radar sin una sola
alerta.

### FP-7 — Un fallo al escribir el log de errores borra todos los conteos del cliente · ALTO

`scripts/reenvio_service/error_log.py:46` define `ensure_error_table` (tabla
`dianenvio_errores`) y **nadie la llama nunca** — verificado por grep en todo el repo. Sus
equivalentes de NC y DS sí se invocan (`reenvio_nc.py:152`, `reenvio_docsoporte.py:121`).

Si la tabla no existe en la DB del scheduler, `insert_error` (`reenvio.py:219`, dentro del
bucle de facturas) lanza `UndefinedTableError`, que es un `asyncpg.PostgresError` y cae en
el handler de la línea 244:

```python
except (asyncpg.PostgresError, OSError) as exc:
    return _summary(key_cli, nombre, total=0, connection_error=str(exc))
```

Resultado: **todo el trabajo ya realizado se descarta** (`total=0`, `exitosas=0`) y el
cliente se reporta en el correo como *"Sin acceso — Falta de permisos de conexión a la base
de datos del cliente"*, que es un diagnóstico completamente equivocado. Facturas realmente
enviadas se reportan como cero.

El mismo patrón se dispara con cualquier error transitorio de Postgres a mitad del bucle.

### FP-8 — `enviar_correo` devuelve el error en vez de fallar · MEDIO

`tasks/envio_correo.py:173` y `:184` hacen `return "Error: …"`. Para Celery la tarea
terminó en **SUCCESS**. Cualquier panel, `flower` o `inspect` que mire el estado verá
tareas verdes aunque no se haya enviado un solo correo (falta `MAIL_HOST`, falta
destinatario, `MAIL_MAILER` incorrecto).

### FP-9 — Un timeout del subprocess DIAN no llega al reporte · MEDIO

`tasks/reenvio_dian.py:290-303`: si `_ejecutar_reenvio` lanza (`TimeoutExpired` a los
1800 s, `FileNotFoundError` del intérprete, etc.) se añade una entrada a `resultados`
—que solo sirve como valor de retorno de la tarea— pero **no se llama a
`_acumular_resultado_dian`**. Esa ejecución no existe para el correo consolidado: ni como
éxito ni como error. Silencio total.

### FP-10 — Filas duplicadas de "Sin acceso" y conteo `errores_cx` incoherente · BAJO

`main.py:222` cuenta `errores_cx` como **key_cli distintos** con error de conexión,
mientras que `errores_conexion` (`:196`) lleva **una entrada por (cliente, tipo)**.
Con la configuración actual (`tipos_doc` con los 3 tipos), un cliente inalcanzable produce
`errores_cx = 1` pero **3 filas idénticas** "Sin acceso — cliente" en el correo
(`tasks/reenvio_dian.py:414`). El texto de esa fila afirma "Falta de permisos de conexión"
para *cualquier* error de conexión, incluida una excepción no controlada
(`main.py:137`, `connection_error='excepcion_no_controlada'`).

---

## 2. Bugs de correctitud y seguridad

### B-1 · ALTO — Dos zonas horarias conviviendo en el flujo DIAN

- Celery/Beat: `America/Mexico_City` (`app.py:25`).
- Claves Redis y etiquetas del reporte DIAN: `America/Bogota`
  (`tasks/reenvio_dian.py:103`, `_fecha_hoy()` en `:130`).

Bogotá va **una hora adelante** de Ciudad de México. Las tareas `reenvio_dian_xc1h` y
`reenvio_dian_xc3h` corren hasta las `23:50` hora MX = `00:50` del día siguiente en Bogotá:
sus resultados se escriben en la clave del **día siguiente**. El reporte consolidado, que
lee `dian:reporte:<hoy en Bogotá>`, mezcla la última ejecución de ayer con las de hoy y
pierde de vista la frontera real de la jornada. La misma división afecta a
`_fecha_hoy()` vs. el `datetime.now()` sin tz de `enviar_correo` (`envio_correo.py:161`),
`monitor_estado_apis.py:41` y `date.today()` de las tareas Siigo/Dominus, que usan la TZ
del sistema operativo, no la de Celery.

### B-2 · ALTO — Comparación de expiración de refresh token con la TZ equivocada

`api/routers/auth.py:100`:

```python
if row["expira_en"].replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
```

`expira_en` es `TIMESTAMPTZ`; psycopg2 devuelve un datetime **aware** en la zona de la
sesión de Postgres. `.replace(tzinfo=utc)` **descarta el offset real** y reinterpreta la
hora local como si fuera UTC. Si la sesión no está en UTC, el token se acepta hasta 5–6
horas **después** de expirado (o se rechaza antes de tiempo, según el signo). Lo correcto
es `.astimezone(timezone.utc)` o comparar directamente los datetimes aware.

### B-3 · CRÍTICO — `JWT_SECRET_KEY` con default vacío

`api/auth.py:10`:

```python
_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "")
```

Comprobado en este entorno: `jwt.encode({...}, "", algorithm="HS256")` **funciona sin
error**. Si la variable no está en el `.env` del servidor (o el `EnvironmentFile` de
systemd no la parsea), la API arranca normalmente y firma/valida con clave vacía —
cualquiera puede generar un token con `rol: admin`. No hay ningún arranque fallido que
avise. Debe ser `os.environ["JWT_SECRET_KEY"]` o un `raise` explícito al importar.

### B-4 · ALTO — kwargs en DB incompatibles con la firma de la función

La API acepta cualquier `kwargs` sin comprobar nada contra la función destino. En la DB
actual hay dos bombas de relojería (ambas tareas están `activa=false` hoy):

| Tarea | kwargs problemático | Error al activarse |
|-------|--------------------|--------------------|
| id 2 `sincronizar_dominus_diario` | `"plantilla": "simple"` | `TypeError: sincronizar_dominus() got an unexpected keyword argument 'plantilla'` |
| id 4 `sincronizar_dominus_mensual` | `"plantilla": "simple"` | idem |
| id 9 `sincronizar_siigo_mes_anterior` | falta `customer_id` (parámetro posicional obligatorio) | `TypeError: sincronizar_siigo() missing 1 required positional argument` |

`sincronizar_dominus` no acepta `plantilla` (`tasks/sincronizar_cliente_dominus.py:99`) y
tampoco lo propaga a `enviar_correo`, así que las dos tareas Dominus **nunca han usado**
la plantilla que su configuración pide.

### B-5 · MEDIO — `env_config` se inyecta a cualquier tarea con credenciales

`app.py:61-63` añade `kwargs["env_config"]` a **toda** tarea que tenga `credentials_id`,
sin verificar que la función lo acepte. Asignar un set de credenciales a
`tasks.correo.enviar_correo` (que no tiene ese parámetro) rompe la tarea con `TypeError`.

Caso real: la tarea id 10 `reporte_dian_diario` tiene `credentials_id = 1`
(**credenciales de Siigo**, que no le sirven de nada). Sobrevive únicamente porque
`enviar_reporte_dian_diario` declara `**_`. Es una asignación errónea que además inyecta
credenciales de Siigo en un contexto que no las necesita.

### B-6 · ALTO — La tarea Siigo revienta sin avisar si faltan credenciales

`tasks/sincronizar_cliente_siigo.py:41-52`: `Config(...)` y `SiigoSync(...)` se construyen
**fuera** del `try` del bucle. Si `env_config` no trae `API_SIIGO_URL`, `SIIGO_USERNAME` o
`SIIGO_ACCESS_KEY`, el constructor lanza `ValueError` (`sync_siigo.py:89-96`) y la tarea
muere **antes de la primera línea de reporte**: no se envía correo, no hay resumen, solo
una traza en el journal. El manejo de errores por proceso, cuidadosamente escrito unas
líneas más abajo, no cubre el fallo más probable.

### B-7 · MEDIO — `POST /tasks/{name}/run` responde 200 para funciones inexistentes

`api/routers/tasks.py:151` hace `send_task(tarea["function"], …)` sin comprobar que la
función esté registrada en el worker. La respuesta es siempre
`{"status": "enqueued"}`. Si `function` tiene un typo (muy fácil: ver D-1), el worker
descarta el mensaje con *"Received unregistered task"* y el operador ve un 200 verde.

### B-8 · MEDIO — `PATCH /tasks` no puede desasignar credenciales

`api/routers/tasks.py:91` usa `model_dump(exclude_none=True)`. Enviar
`{"credentials_id": null}` para quitar el set de credenciales **se ignora en silencio** y
devuelve 200 con la fila intacta. Lo mismo aplica a cualquier campo que se quiera poner a
`NULL`. La documentación dice "solo los campos enviados se modifican", lo que sugiere
justo lo contrario.

### B-9 · MEDIO — SMTP sin timeout ni soporte SSL implícito

`tasks/envio_correo.py:209`: `smtplib.SMTP(host, port)` sin `timeout`. Un servidor SMTP que
acepta la conexión y no responde deja el worker **colgado indefinidamente**, ocupando un
slot de concurrencia. Además, `usar_tls` solo contempla `tls`/`starttls`
(`:168-169`): con `MAIL_ENCRYPTION=ssl` intentaría SMTP en claro contra el 465. No hay
reintento (`autoretry_for`) ante fallos transitorios: un correo perdido es un reporte
perdido.

### B-10 · MEDIO — HTML del correo sin escapar

`_construir_html` (`tasks/envio_correo.py:36-155`) interpola `mensaje`, `r["proceso"]` y
`r["detalle"]` directamente en el HTML. Los detalles vienen de respuestas de API y de
mensajes de error de la DIAN, que contienen XML y comillas: cualquier `<` rompe la tabla,
y un `</td>` en un mensaje de error destroza el layout del correo. Falta un
`html.escape()`.

### B-11 · BAJO — `_cargar_plantilla` usa `str.format` sobre el archivo completo

`tasks/envio_correo.py:22`. `templates/simple.html` funciona porque hoy solo tiene tres
llaves, todas placeholders. Cualquier plantilla futura con CSS (`{margin:0}`) o JS lanzará
`KeyError`/`IndexError`, y el `except` de la línea 205 **solo captura
`FileNotFoundError`** → la tarea de correo falla entera.

### B-12 · BAJO — `enmascarar` no recorre listas

`api/deps.py:16-25` solo desciende en `dict`. `kwargs` como
`{"apis": [{"api_key": "secreto"}]}` (estructura perfectamente válida, y de hecho la
tarea `monitor_apis` guarda una lista de dicts en `kwargs`) se devuelven **en claro** por
`GET /tasks`. El test `test/test_enmascarar.py` no cubre este caso.

### B-13 · MEDIO — Interpolación directa de los filtros de fecha en SQL

`scripts/reenvio_service/config.py:52-69` construye el `WHERE` concatenando `FILTRO_DIA`,
`FILTRO_MES`, `FILTRO_DESDE`, `FILTRO_HASTA` (solo `FILTRO_ANIO` pasa por `int()`). El
docstring justifica que "vienen de variables de entorno, no de input de usuario", pero eso
ya no es cierto: llegan desde `scheduler_credentials`, editable con
`PATCH /credentials/{id}` por cualquier usuario `admin` de la API. La consulta se ejecuta
contra la **DB de cada cliente**. No es explotable desde fuera, pero es una inyección real
a un paso de distancia y contradice el resto del código, que sí usa parámetros.

### B-14 · BAJO — `require_role` con varios roles concede el menor

`api/deps.py:81`: `nivel_requerido = min(...)`. `require_role("admin", "operator")` deja
pasar a un operator. Hoy siempre se llama con un solo rol, así que no hay impacto, pero el
nombre y la firma invitan al error.

### B-15 · BAJO — Importar `app` abre una conexión a Postgres

`app.py:77` ejecuta `construir_schedule()` en tiempo de importación. Cualquier
`import app` —`beat_scheduler.py`, `scripts/estado.sh`, un test unitario, el `--help` de
una herramienta— **golpea la base de datos de producción**. Ejecutar el autocheck de
`beat_scheduler.py` en esta auditoría abrió una conexión real.

### B-16 · MEDIO — Autenticación sin límite de intentos ni limpieza de tokens

`/auth/login` (`api/routers/auth.py:30`) no tiene rate limiting ni bloqueo por intentos
fallidos: fuerza bruta libre contra bcrypt. `scheduler_refresh_tokens` no se purga nunca —
ni expirados ni revocados—, así que crece de forma monótona; cada login inserta una fila.

### B-17 · BAJO — La tarea Siigo autentica cinco veces por corrida

`run_sync` pide token de API y abre sesión Siigo en cada llamada
(`sync_siigo.py:372-379`), y la tarea la invoca una vez por proceso
(`tasks/sincronizar_cliente_siigo.py:56-62`). Con los 5 procesos por defecto son
**5 pares de autenticaciones** por ejecución. El commit `08c411d` ("pedir el token una
sola vez por corrida") lo resolvió a nivel de `run_sync`, no a nivel de tarea.

### B-18 · BAJO — Reanudar desde una página intermedia borra los datos previos

`sync_siigo.py:285`: `clear = not unique`. Si se llama con `page=5`, la petición sigue
llevando `clear=true` y limpia todo el rango antes de escribir solo desde la página 5. El
comentario de la línea 301 ("solo la página 1 limpia") deja de ser cierto cuando
`start_page > 1`.

### B-19 · BAJO — `wait_for(queue.get())` puede perder un ítem

`sync_siigo.py:318`. Patrón conocido: si el timeout y la entrega del ítem compiten, la
cancelación de `get()` puede descartar el elemento ya extraído. Con la cola precargada
antes de arrancar los workers el riesgo es bajo, pero se manifestaría como páginas
perdidas silenciosamente (y por FP-3, sin reflejarse en el reporte).

### B-20 · MEDIO — `cufe: null` tumba la corrida completa de un cliente

`reenvio_nc.py:189` y `reenvio_docsoporte.py:160`: `result.get('cufe', '')[:20]`. El
default solo aplica si la clave **falta**; si la API devuelve `"cufe": null` el resultado
es `None[:20]` → `TypeError`, capturado por el `except Exception` genérico
(`reenvio_nc.py:229`) → se devuelve `total=0` + `connection_error`, perdiendo todos los
conteos. Mismo patrón destructivo que FP-7.

### B-21 · BAJO — Scripts de operación desalineados

`scripts/detener.sh` para `celery-worker` y `celery-beat` pero **no `celery-api`**, aunque
`reiniciar.sh` sí lo reinicia. `estado.sh` tampoco muestra el estado de la API. Un
"detener" no deja el sistema detenido.

### B-22 · BAJO — Puerto de la API distinto en systemd y en la documentación

`systemd/celery-api.service` usa `--port 8014`. README y CLAUDE.md documentan **8080** en
todos sus ejemplos y en el enlace a `/docs`.

### B-23 · BAJO — Detección de la columna `prefijo` sin filtrar por esquema

`reenvio.py:27-30` consulta `information_schema.columns` con
`table_name = 'facturas'` y sin `table_schema`. Si la DB del cliente tiene otra tabla
`facturas` en un esquema secundario (backups, staging), se elige la variante equivocada de
la consulta y se leen documentos que no corresponden.

### B-24 · BAJO — Errores de Postgres expuestos por HTTP

`api/routers/tasks.py:85` y `api/routers/credentials.py:59,92` devuelven
`detail=str(e)` de psycopg2: nombres de tablas, constraints y fragmentos de SQL llegan al
cliente. Con auth de por medio el riesgo es limitado, pero es información innecesaria.

---

## 3. Inconsistencias de documentación

### D-1 · ALTO — Los nombres de tarea documentados no existen

Los `name` reales de los decoradores no coinciden con la ruta del módulo, y la
documentación usa la ruta del módulo:

| Documentado (README / CLAUDE.md) | Real (`@celery_app.task(name=…)`) |
|---|---|
| `tasks.sincronizar_cliente_siigo.sincronizar_siigo` | `tasks.siigo.sincronizar_siigo` |
| `tasks.sincronizar_cliente_dominus.sincronizar_dominus` | `tasks.dominus.sincronizar_dominus` |
| `tasks.envio_correo.enviar_correo` | `tasks.correo.enviar_correo` |
| `tasks.monitor_estado_apis.verificar_apis` | `tasks.monitor.verificar_apis` |

Aparecen mal en la tabla "Tareas disponibles" del README, en el ejemplo `POST /tasks`
del README ("Caso 1") y en la sección "Ejecutar tareas manualmente" de CLAUDE.md. Copiar
cualquiera de esos ejemplos crea una tarea que la API acepta (B-7), Beat encola y el
worker descarta como *unregistered*: **fallo silencioso por documentación**.

Solo `tasks.reenvio_dian.*` está documentado correctamente.

### D-2 · ALTO — `scripts/migrar_db.py` no existe

Es el paso 3 de la instalación tanto en README como en CLAUDE.md, pero está listado en
`.gitignore` y no está en el árbol. Una instalación nueva siguiendo el README no puede
crear las tablas. `scripts/crear_usuarios.py` está en la misma situación (ignorado), aunque
ahí el README sí lo advierte.

### D-3 · MEDIO — `.github/copilot-instructions.md` describe un proyecto que ya no existe

Menciona `config/tasks.yaml` como "source of truth", `tasks/scripts.py`,
`registrar_tareas()` y `yaml.safe_load`. Nada de eso existe: la configuración vive en
PostgreSQL desde hace varios refactors, y `pyyaml` ya no se usa. Un agente que siga estas
instrucciones producirá código incompatible.

### D-4 · MEDIO — El ejemplo de credenciales DIAN del README está incompleto

El `POST /credentials` de la sección "Configurar reenvío DIAN" incluye `MAIN_DB_*`,
`PROVEEDOR_INTEGRACION` y `MAX_INTENTOS`, pero **omite `API_PYTHON_URL`,
`API_PYTHON_USERNAME` y `API_PYTHON_PASSWORD`**, que `config.py:18-20` lee con
`os.environ[...]`. Siguiendo el README al pie de la letra, el subprocess muere con
`KeyError` al importar. (El set real en producción, `esuite_dian` id 3, sí las tiene.)

### D-5 · MEDIO — Hora del reporte DIAN: 17:00 en tres sitios, 18:00 en la DB

`tasks/reenvio_dian.py:16` y `:337`, y `docs/reenvio_dian.md` dicen 17:00. La tarea real
está a las 18:00 (y solo el día 1, ver FP-1).

### D-6 · MEDIO — CLAUDE.md contradice al README sobre los reinicios

CLAUDE.md ("Agregar una tarea nueva") sigue diciendo `POST /tasks … → reiniciar servicios`
para el caso de función existente. Con `SchedulerDB` eso ya no hace falta (el README lo
dice bien: "Beat la toma sola en menos de 60s"). La propia sección de arquitectura de
CLAUDE.md describe el scheduler custom, así que el documento se contradice a sí mismo.

### D-7 · MEDIO — CLAUDE.md no documenta la autenticación

No menciona `/auth/*`, ni los roles, ni las tablas `scheduler_users` /
`scheduler_refresh_tokens`, ni `JWT_SECRET_KEY` / `ACCESS_TOKEN_EXPIRE_MINUTES` /
`REFRESH_TOKEN_EXPIRE_DAYS` en la tabla de variables de entorno. Su tabla de endpoints
sugiere que la API es abierta. El README sí lo cubre.

### D-8 · BAJO — Referencias a `esuite_dian_app_v2/`

README ("Los imports de `app.*` se resuelven … desde `esuite_dian_app_v2/`") y el diagrama
de `docs/reenvio_dian.md` apuntan a una ruta que ya no se usa: el servicio vive en
`scripts/reenvio_service/` y no importa nada de `app.*`.

### D-9 · BAJO — El docstring de `error_log.py` miente sobre la ubicación de las tablas

Dice "tablas de errores … en la DB de cada cliente"; los tres `insert_*` se ejecutan
contra `scheduler_pool`, es decir la **DB del scheduler**. Relevante para cualquiera que
vaya a consultar los errores.

### D-10 · BAJO — Default de `tipo_doc` documentado pero no implementado

El docstring del módulo (`tasks/reenvio_dian.py:34`) dice `tipo_doc … Default: "facturas"`,
pero la firma es `tipo_doc: str = None` (`:248`). Invocar
`reenviar_documentos_dian()` sin kwargs lanza `ValueError: tipo(s) inválido(s): [None]`.
Solo el alias `reenviar_facturas_dian` tiene el default documentado.

---

## 4. Código muerto y dependencias sin uso

| Elemento | Estado |
|----------|--------|
| `scripts/siigo_script.py` (479 líneas) | Reemplazado por `scripts/sync_siigo.py`. Sin una sola referencia en el repo. Es el único consumidor de `rich`. |
| `error_log.ensure_error_table` | Definida y nunca llamada — y hace falta (ver FP-7). |
| `sqlalchemy` en `requirements.txt` | Cero imports en todo el repo. |
| `pyyaml` en `requirements.txt` | Cero imports; resto del esquema `config/tasks.yaml` eliminado. |
| `rich` en `requirements.txt` | Solo lo usa el script muerto. |

`celerybeat-schedule` (binario del estado de Beat) está en el directorio de trabajo pero
correctamente ignorado por git.

---

## 5. Lo que está bien

Para no dejar solo la lista de defectos:

- `beat_scheduler.SchedulerDB` es una solución limpia y bien acotada: `merge_inplace`
  preserva `last_run_at`, el fallo de lectura mantiene el schedule vigente en lugar de
  vaciarlo, y trae su propio autocheck ejecutable.
- El flujo de refresh tokens implementa **rotación con detección de reuso** (revoca todas
  las sesiones del usuario ante un token ya usado) y guarda solo el SHA-256. Es más de lo
  habitual en proyectos de este tamaño.
- Los refactors recientes de Siigo (`SiigoSync`) validan entradas en el constructor con
  mensajes claros y aíslan el fallo de un proceso del resto.
- Los tres checks de `test/` se ejecutan y pasan (`test_sync_siigo.py`,
  `test_enmascarar.py`, autocheck de `beat_scheduler.py`).
- `enmascarar` con la regla "sufijo `key`" evita el falso enmascaramiento de `key_cli` /
  `key_clis`, con test que lo cubre.
- El manejo de errores por cliente y por tipo en `reenvio_service/main.py` impide que un
  cliente caído aborte a los demás.

---

## 6. Orden de trabajo sugerido

**Inmediato (config, sin desplegar código)**

1. FP-1 — `PATCH /tasks/reporte_dian_diario {"day_of_month": "*", "hour": "17"}`.
2. B-5 — quitar `credentials_id` de `reporte_dian_diario` (hoy apunta a las credenciales
   de Siigo).
3. B-3 — verificar que `JWT_SECRET_KEY` esté efectivamente cargada en el proceso
   `celery-api` del servidor.
4. B-4 — limpiar el kwarg `plantilla` de las dos tareas Dominus y añadir `customer_id` a
   `sincronizar_siigo_mes_anterior` **antes** de activarlas.

**Corto plazo (correctitud del reporte)**

5. FP-2 — `sys.exit(1)` en los cinco `return` fatales de `main.py` y emitir siempre
   `RESUMEN_JSON`.
6. FP-7 / B-20 — llamar a `ensure_error_table`; sacar el `insert_error` del camino que
   destruye los conteos (try/except local); usar `(result.get('cufe') or '')[:20]`.
7. FP-9 — acumular en Redis también los timeouts y excepciones del subprocess.
8. FP-3 — que los workers de `sync_siigo` reporten fallos de páginas y que el estado del
   proceso lo refleje.
9. FP-4 — unificar el tratamiento de `ALREADY_EMITTED` y llevar `fallidas_detalle` a NC y DS.
10. B-1 — una sola zona horaria para todo el flujo DIAN.

**Higiene**

11. B-2, B-9, B-10, B-16.
12. D-1 y D-2 (documentación que induce a errores silenciosos), luego el resto de la
    sección 3.
13. Borrar `scripts/siigo_script.py` y las tres dependencias sin uso.

---

## Estado de las correcciones

Aplicadas el 2026-08-09 sobre la rama `siigo-sync-refactor`.

### Cambios estructurales previos a los arreglos

| # | Cambio | Por qué |
|---|--------|---------|
| M1 | `tasks/{sincronizar_cliente_siigo,sincronizar_cliente_dominus,envio_correo,monitor_estado_apis}.py` → `tasks/{siigo,dominus,correo,monitor}.py` | El nombre del módulo ahora coincide con el del decorador. El `name` no cambió, así que `scheduler_tasks.function` sigue siendo válida sin tocar la DB. Elimina D-1 por construcción. |
| M2 | `app.py` ya no llama a `construir_schedule()` al importarse; lo hace `SchedulerDB.setup_schedule()` | Importar `app` deja de abrir Postgres, lo que permite que la API lea el registro de tareas (M3) y que los checks importen sin DB. |
| M3 | `_validar_llamada()` en `api/routers/tasks.py`, usada en POST, PATCH y `/run` | La DB ya no puede pedir algo que Python no sabe hacer. `construir_schedule()` además ignora, con log de error, cualquier fila cuya función no esté registrada. |
| M4 | `scripts/reenvio_service/comun.py` | Las piezas triplicadas que ya habían divergido (clasificación de respuesta, parser de error HTTP, resumen, registro de error) viven una sola vez. |
| M5 | Borrado `scripts/siigo_script.py` (479 líneas muertas), quitadas `sqlalchemy`/`pyyaml`/`rich`, `test/` fuera de `.gitignore` | Los checks no protegen nada si no están versionados. |
| M6 | `scripts/` dividido por dominio en `etl/`, `reenvio/` y `ops/` | Una sola carpeta mezclaba librería in-process, dos entry points de subprocess, tres shell de operación y un script de admin. Ahora la división es funcional: cargar datos, reenviar datos, operar el servicio. |

### M6 — Correspondencia de rutas

| Antes | Ahora |
|-------|-------|
| `scripts/sync_siigo.py` | `etl/siigo.py` |
| `scripts/dominus_script.py` | `etl/dominus.py` |
| `scripts/reenvio_service/main.py` | `reenvio/main.py` |
| `scripts/reenvio_service/reenvio.py` | `reenvio/facturas.py` |
| `scripts/reenvio_service/reenvio_nc.py` | `reenvio/notas_credito.py` |
| `scripts/reenvio_service/reenvio_docsoporte.py` | `reenvio/docsoporte.py` |
| `scripts/reenvio_service/db_clientes.py` | `reenvio/clientes.py` |
| `scripts/reenvio_service/error_log.py` | `reenvio/errores.py` |
| `scripts/reenvio_service/{config,comun}.py` | `reenvio/{config,comun}.py` |
| `scripts/{reiniciar,detener,estado}.sh`, `crear_usuarios.py` | `ops/` |

Los dos entry points de subprocess se ejecutan ahora igual —`python -m <modulo>` desde
la raíz— a través de `tasks/ejecutar.py`, que unifica la búsqueda del intérprete (las
dos copias anteriores ya habían divergido en el orden de candidatos) y la inyección de
`env_config`. Desaparecen los cálculos de rutas de archivo y el `cwd` a mano.

### Falsos positivos

| ID | Estado | Dónde |
|----|--------|-------|
| FP-1 | **Pendiente (DB)** | Requiere `PATCH /tasks/reporte_dian_diario {"day_of_month": "*"}`. No se toca la configuración de producción sin visto bueno. |
| FP-2 | Corregido | `main.py`: los cinco fallos fatales devuelven 1 y emiten `RESUMEN_JSON` con campo `error`; la fuga de `scheduler_pool` también. `_parsear_resumen` marca `error` cuando la línea no aparece. El reporte lo muestra como "No se pudo ejecutar" y fuerza "Con errores". |
| FP-3 | Corregido | `_process_queue` cuenta páginas fallidas por proceso; un proceso con páginas caídas pasa a `failed`. Nuevos campos `paginas_ok` / `paginas_fallidas`. |
| FP-4 | Corregido | `comun.clasificar()`: `ALREADY_EMITTED` es su propia categoría (`ya_emitidas`) en los tres tipos, ni éxito ni fallo. NC y DS ahora construyen `fallidas_detalle`. |
| FP-5 | Corregido | El monitor exige `resp.ok` (<400) en vez de `<500`. |
| FP-6 | Corregido | Nuevo contador `agotadas` (documentos sin intentos restantes): consulta `COUNT` en facturas y NC, contador propio en DS. La fila del cliente sale ERROR si los hay y el total aparece en el correo. |
| FP-7 | Corregido | `ensure_error_table` se llama; `comun.registrar_error()` aísla el fallo del log para que no destruya los conteos ni invente un error de conexión. |
| FP-8 | Corregido | `enviar_correo` levanta excepción en lugar de devolver un string, con `autoretry_for` para cortes transitorios. |
| FP-9 | Corregido | El `except` del subprocess acumula en Redis un `CompletedProcess` sintético: los timeouts ya no desaparecen del reporte. |
| FP-10 | Corregido | Deduplicación por cliente en `main.py` y otra vez en `_consolidar`. Además, una sola fila por cliente y tipo en lugar de una por corrida. |

### Bugs

| ID | Estado | Nota |
|----|--------|------|
| B-1 | Corregido | `app.ahora()` es el único reloj; `_TZ` hardcodeada eliminada. **Decisión pendiente**: si la operación es colombiana, cambiar `timezone` en `app.py` a `America/Bogota` mueve todo a la vez (y corre todos los crons una hora). |
| B-2 | Corregido | `astimezone(timezone.utc)` en lugar de `replace`. |
| B-3 | Corregido | `api/auth.py` exige `JWT_SECRET_KEY` de ≥32 caracteres al importar. **Atención**: el `.env` de este repo la tiene vacía, así que la API no arrancará hasta generarla. |
| B-4 | Parcial | `sincronizar_dominus*` ya acepta `plantilla` y la propaga al correo. Falta añadir `customer_id` a `sincronizar_siigo_mes_anterior` en la DB. |
| B-5 | Corregido (detección) | La validación rechaza asignar credenciales a una función sin `env_config`. Queda quitar `credentials_id=1` de `reporte_dian_diario` en la DB. |
| B-6 | Corregido | La construcción de `SiigoSync` está dentro de un `try` que reporta por correo antes de propagar. |
| B-7 | Corregido | `/run` valida antes de encolar. |
| B-8 | Corregido | `exclude_unset` + rechazo explícito de `null` en campos no anulables. |
| B-9 | Corregido | `timeout=30`, `SMTP_SSL` cuando `MAIL_ENCRYPTION=ssl`, reintentos automáticos. |
| B-10 | Corregido | `html.escape()` en asunto, mensaje, proceso, detalle y campos del resumen. |
| B-11 | Corregido | `_cargar_plantilla` sustituye literalmente en vez de `str.format`. |
| B-12 | Corregido | `enmascarar` desciende por listas. |
| B-13 | Corregido | Los `FILTRO_*` se validan con regex al cargarse. |
| B-14 | Corregido | `max()` en vez de `min()`. |
| B-15 | Corregido | Ver M2. |
| B-16 | Parcial | Se purgan los refresh tokens vencidos hace más de 7 días en cada login. **Sin rate limiting en `/auth/login`**: hace falta a nivel de proxy o una dependencia nueva; no se añadió. |
| B-17 | **No corregido a propósito** | Un token por `run_sync` (5 por corrida) protege contra la expiración a los ~5 minutos en sincronizaciones largas. Reusarlo entre procesos ahorraría 4 llamadas y arriesgaría un 401 a mitad. |
| B-18 | Corregido | `clear = not unique and page == 1`. |
| B-19 | Corregido | Centinelas en la cola en vez de `wait_for(get(), 1.0)`; de paso desaparecen el `join()` y los `cancel()`. |
| B-20 | Corregido | `comun.cufe()` tolera `null`. |
| B-21 | Corregido | `detener.sh` y `estado.sh` incluyen `celery-api`. |
| B-22 | Documentado | Se mantiene 8014 en systemd (puede haber un proxy apuntando ahí) y se documenta 8080 dev / 8014 producción. |
| B-23 | Corregido | `table_schema = current_schema()`. |
| B-24 | Corregido | `api.deps.error_db()` devuelve solo el mensaje primario. |

### Documentación

D-1, D-2, D-4, D-5, D-6, D-7, D-8, D-9 y D-10 corregidos en README, CLAUDE.md,
`docs/reenvio_dian.md`, `docs/conexiones_api_dian.md` y los docstrings afectados.
D-3 (`.github/copilot-instructions.md`) se reescribió por completo.

Las horas concretas ("17:00") desaparecieron de la documentación: la fuente de verdad
es `scheduler_tasks`, y fijarlas en prosa fue justo lo que produjo D-5.

### Checks nuevos

```
test/test_reenvio_comun.py     clasificación, cufe nulo, errores HTTP, resumen
test/test_reporte_dian.py      arranque fallido, deltas vs saldos, dedup de clientes
test/test_validar_llamada.py   función inexistente, kwargs de más, faltantes, env_config
test/test_sync_siigo.py        (+) una página caída invalida el proceso
```

Los siete checks pasan (`for t in test/*.py; do PYTHONPATH=. .venv/bin/python "$t"; done`).

### Pendiente, requiere decisión

1. **FP-1** — `day_of_month = 1` en `reporte_dian_diario`: el reporte es mensual, no diario.
2. **B-3** — `JWT_SECRET_KEY` vacía en el `.env`. La API **no arrancará** hasta generarla.
   Mientras estuvo vacía, cualquiera con acceso al puerto podía firmar un token de admin.
3. **B-4** — `sincronizar_siigo_mes_anterior` sigue sin `customer_id` en sus kwargs.
4. **B-5** — `reporte_dian_diario` sigue con `credentials_id = 1` (credenciales de Siigo).
5. **B-1** — elegir zona horaria única para la operación (`America/Mexico_City` actual vs.
   `America/Bogota`, que es donde están la DIAN, Siigo y los clientes).
6. **B-16** — rate limiting en `/auth/login`.
7. **D-2** — `migrar_db.py` y `crear_usuarios.py` siguen fuera del repositorio: el DDL de
   las tablas de auth solo existe en un archivo no versionado.
