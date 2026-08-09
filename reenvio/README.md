# reenvio/ — Reenvío de documentos

Reintento de envíos que un sistema externo todavía no aceptó. Hoy solo DIAN.

```
python -m reenvio.main --tipo {facturas,nc,docsoporte} [--key-cli KEY]
```

Lo lanza `tasks/reenvio_dian.py` como subprocess, una vez por cada combinación
(cliente, tipo). El resultado viaja de vuelta en una única línea de stdout:

```
RESUMEN_JSON:{"exitosas": 3, "fallidas": 1, ...}
```

| Módulo | Qué hace |
|--------|----------|
| `main.py` | Orquesta: autentica, lee los clientes y recorre los tres flujos |
| `config.py` | Variables de entorno (llegan de `scheduler_credentials`) y filtros de fecha |
| `clientes.py` | Directorio de clientes en la DB maestra eSuite |
| `comun.py` | **Lo compartido por los tres flujos**: llamada a la API, clasificación de la respuesta, resumen, registro de errores |
| `facturas.py` / `notas_credito.py` / `docsoporte.py` | Lo específico de cada tipo: consulta de pendientes y endpoint |
| `errores.py` | Tablas `dianenvio_errores*` en la DB del scheduler |

## Reglas que no hay que romper

- **Lo común va en `comun.py`.** Los tres flujos nacieron copiando el primero y
  divergieron: `ALREADY_EMITTED` acabó contando distinto en cada uno. Si un
  arreglo hay que hacerlo tres veces, va en el sitio equivocado.
- **`main.py` sale con código ≠ 0 si no pudo trabajar**, y emite `RESUMEN_JSON`
  igual, con campo `error`. Un fallo silencioso se reporta como jornada limpia.
- **Distinguir deltas de saldos.** `exitosas`/`fallidas` son de esa corrida y se
  suman; `agotadas` es el saldo pendiente y no se acumula entre corridas.

## Documentos atascados

Un documento que gasta `MAX_INTENTOS` desaparece de la consulta de pendientes y
no se reintenta solo nunca más. Cada corrida los recoge igualmente
(`agotadas_detalle`), cruzando el estado real en la DB del cliente con el último
error registrado en `dianenvio_errores*`, y `comun.clasificar_causa()` los separa
en tres:

| Causa | Ejemplo real | Qué hacer |
|-------|--------------|-----------|
| `datos` | `NC_NO_SERIAL` · "La nota crédito no tiene resolución (serial_id) asignada" | Completar el dato y reiniciar el contador |
| `dian` | `137` · "Regla DSAJ24a, Rechazo: No está informado el DV del NIT" | Corregir el documento |
| `tecnica` | `HTTP_500`, o un `137` cuyo mensaje es "Intente más tarde, Documento en proceso" | Reintentable tal cual |

La clasificación mira **código y mensaje**: hay códigos de validación DIAN cuyo
mensaje es transitorio, y clasificarlos por código los condenaría a no
reintentarse jamás. Las listas de códigos y patrones están al principio de
`comun.py`; ampliarlas ahí cuando aparezca un código nuevo.

## Agregar un tipo de documento

1. Módulo nuevo con su consulta de pendientes y su `_procesar_cliente`, usando
   `comun.llamar_api()` y `comun.resumen()`.
2. Añadirlo a `TIPOS_VALIDOS` y al recorrido de `main.py`.
3. Registrar la clave en `_TIPOS_CLI_DIAN` y `_ETIQUETAS_DIAN` de
   `tasks/reenvio_dian.py`.
