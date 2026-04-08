# Servicio de Reenvío Automático DIAN

Servicio externo e independiente que reenvía automáticamente facturas pendientes a la DIAN.
Corre por separado del app principal (`esuite_dian_app_v2`) y puede ejecutarse manualmente o como cron job.

---

## Estructura del proyecto

```
reenvio_service/
├── main.py          → Punto de entrada. Orquesta el proceso completo.
├── config.py        → Variables de entorno y configuración.
├── db_clientes.py   → Conexión a la DB principal para leer clientes registrados.
├── reenvio.py       → Lógica central de reenvío por cliente.
└── error_log.py     → Tabla dianenvio_errores y registro de fallos.

.env.reenvio         → Variables de entorno del servicio (no subir a git).
.env.reenvio.example → Template de configuración.
```

---

## Configuración

Copiar el template y completar los valores:

```bash
cp .env.reenvio.example .env.reenvio
```

### Variables disponibles

| Variable | Descripción | Requerida |
|----------|-------------|-----------|
| `MAIN_DB_HOST` | Host de la DB principal de esuite | Sí |
| `MAIN_DB_PORT` | Puerto (defecto: 5432) | No |
| `MAIN_DB_NAME` | Nombre de la DB principal | Sí |
| `MAIN_DB_USER` | Usuario de la DB principal | Sí |
| `MAIN_DB_PASSWORD` | Contraseña de la DB principal | Sí |
| `PROVEEDOR_INTEGRACION` | Proveedor DIAN (defecto: `AVIA`) | No |
| `MAX_INTENTOS` | Máximo de intentos por factura (defecto: `3`) | No |
| `KEY_CLI_FILTER` | Procesar solo este cliente. Vacío = todos | No |
| `FILTRO_DIA` | Filtrar facturas de un día: `YYYY-MM-DD` | No |
| `FILTRO_MES` | Filtrar facturas de un mes: `YYYY-MM` | No |
| `FILTRO_ANIO` | Filtrar facturas de un año: `YYYY` | No |
| `FILTRO_DESDE` | Inicio del rango de fecha: `YYYY-MM-DD` | No |
| `FILTRO_HASTA` | Fin del rango de fecha: `YYYY-MM-DD` | No |

> Los filtros de fecha son mutuamente excluyentes (día, mes, año o rango).
> Si no se especifica ninguno, el **defecto es el mes actual**.

---

## Uso

### Ejecución básica (mes actual, todos los clientes)

```bash
python3 -m reenvio_service.main
```

### Solo un cliente específico

```bash
python3 -m reenvio_service.main --key-cli 00000000
```

### Filtros de fecha

```bash
# Facturas de hoy
python3 -m reenvio_service.main --dia 2026-03-30

# Facturas de un mes específico
python3 -m reenvio_service.main --mes 2026-02

# Facturas de un año completo
python3 -m reenvio_service.main --anio 2026

# Rango de fechas personalizado
python3 -m reenvio_service.main --desde 2026-03-01 --hasta 2026-03-15

# Combinar cliente + filtro de fecha
python3 -m reenvio_service.main --key-cli 00000000 --mes 2026-03
```

### Cron job (ejemplo: cada hora)

```bash
0 * * * * cd /ruta/al/proyecto && python3 -m reenvio_service.main >> /var/log/reenvio_dian.log 2>&1
```

---

## Cómo funciona

### Flujo general

```
main.py
  │
  ├── 1. Inicializa el servicio de homologación DIAN (obligatorio para getCufe)
  ├── 2. Conecta a la DB principal → obtiene clientes con transmitir=true
  └── 3. Para cada cliente:
            │
            ├── Conecta a la DB del cliente (asyncpg)
            ├── Crea tabla dianenvio_errores si no existe
            ├── Consulta facturas pendientes con el filtro de fecha activo
            └── Para cada factura pendiente:
                      ├── Llama getCufe() del app principal
                      ├── OK  → cuenta como exitosa
                      └── FAIL → inserta fila en dianenvio_errores
```

### Filtro de facturas pendientes

Una factura se considera pendiente si cumple todas estas condiciones:

```sql
WHERE modalidadpago_id = 2          -- es factura electrónica
  AND diancufe IS NULL               -- nunca fue aceptada por la DIAN
  AND COALESCE(diannumeroenvios, 0) < 3   -- menos de 3 intentos
  AND created_at >= <desde>          -- dentro del rango de fecha activo
  AND created_at <  <hasta>
```

### Control de intentos (`diannumeroenvios`)

- `getCufe()` incrementa `diannumeroenvios` **antes** de enviar, sin importar si el envío falla.
- Al tercer fallo el campo queda en `3` → la factura nunca vuelve a aparecer en el filtro.
- Para reintentar manualmente más de 3 veces hay que ejecutar directamente en la DB del cliente:

```sql
UPDATE facturas SET diannumeroenvios = 0 WHERE id = <id>;
```

### Detección de columna `prefijo`

Algunas DBs de clientes tienen `prefijo` directamente en `facturas`; otras lo almacenan en `clienteserialfacturas`. El servicio detecta automáticamente cuál usar:

```python
# Si facturas.prefijo existe → query directo
# Si no existe              → JOIN con clienteserialfacturas
```

### Protección contra ejecuciones concurrentes

Cada cliente tiene un `asyncio.Lock` propio. Si el servicio se invoca dos veces al mismo tiempo para el mismo cliente, la segunda espera a que termine la primera, evitando doble-incremento de `diannumeroenvios`.

---

## Log de errores (`dianenvio_errores`)

Cuando un envío falla, el error se registra en la tabla `dianenvio_errores` dentro de la **DB del cliente** (junto a `dianenviofacturas`). La tabla se crea automáticamente al primer uso.

### Esquema de la tabla

| Columna | Tipo | Descripción |
|---------|------|-------------|
| `id` | SERIAL | PK autoincremental |
| `factura_id` | INTEGER | ID de la factura en `facturas` |
| `prefijo` | VARCHAR(20) | Prefijo de la factura |
| `consecutivo` | INTEGER | Consecutivo de la factura |
| `intento_numero` | INTEGER | Número de intento que falló (1, 2 o 3) |
| `error_codigo` | VARCHAR(100) | `reasonCode` retornado por `getCufe` |
| `error_razon` | VARCHAR(500) | `reason` retornado por `getCufe` |
| `error_mensaje` | TEXT | JSON con `message` y `detail` del error |
| `cliente_key` | VARCHAR(100) | `key_cli` del cliente |
| `created_at` | TIMESTAMPTZ | Fecha y hora del error |

### Códigos de error comunes

| Código | Causa |
|--------|-------|
| `INVOICE_NOT_FOUND` | La factura no existe en la DB del cliente |
| `SUPPLIER_NOT_FOUND` | El proveedor de la factura no está configurado |
| `TRANSMISSION_DISABLED` | El cliente tiene `transmitir=False` |
| `API_ERROR` | Error de comunicación con la API de la DIAN |
| `DATABASE_ERROR` | Error de conexión o consulta en la DB del cliente |
| `UNEXPECTED_ERROR` | Excepción no controlada dentro de `getCufe` |
| `142` | La factura ya fue procesada anteriormente en la DIAN |

---

## Dependencias

El servicio reutiliza módulos del app principal:

- `app.api.routes.v1.functions.contable.facturacionAvia_refactored.getCufe` — función de envío a DIAN
- `app.schemas.clientesConexionesDB.ClientesConexionesDBBase` — schema de conexión de cliente
- `app.services.homologacion.init_homologacion_service_from_env` — inicialización del proveedor DIAN

Por esto debe ejecutarse desde la raíz del proyecto `esuite_dian_app_v2`.
