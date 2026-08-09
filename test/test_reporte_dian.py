"""Check de la consolidación del reporte DIAN. Ejecutar: python test/test_reporte_dian.py

Cubre los falsos positivos que motivaron el cambio: fallo de arranque
reportado como jornada limpia, conteos multiplicados por corrida y clientes
sin acceso duplicados.
"""

from tasks.reenvio_dian import _consolidar, _construir_reporte, _construir_reporte_atascados


def _entrada(etiqueta="facturas", exito=True, returncode=0, key_cli="c1", **resumen):
    base = {
        "clientes": 1, "facturas": 0, "exitosas": 0, "fallidas": 0,
        "errores_cx": 0, "fallidas_detalle": [], "errores_conexion": [],
        "resultados_por_cliente": [],
    }
    base.update(resumen)
    return {
        "tipo_doc": "facturas", "etiqueta": etiqueta, "key_cli": key_cli,
        "exito": exito, "returncode": returncode, "resumen": base,
        "stderr": "", "timestamp": "2026-08-09T10:00:00-05:00",
    }


def _cliente(nombre="Cliente Uno", tipo="facturas", **conteos):
    d = {"cliente": nombre, "tipo": tipo, "exitosas": 0, "fallidas": 0,
         "omitidas": 0, "ya_emitidas": 0, "agotadas": 0}
    d.update(conteos)
    return d


def test_fallo_de_arranque_no_es_jornada_limpia():
    entradas = [_entrada(exito=False, returncode=1, error="Sin conexión a la DB maestra")]
    _, filas, estado = _construir_reporte(_consolidar(entradas), "09/08/2026")

    assert estado == "Con errores", "un arranque fallido no puede reportarse como Sin errores"
    assert any("No se pudo ejecutar" in f["proceso"] for f in filas), filas
    # No debe duplicarse como "error de ejecución" genérico
    assert not any("Error de ejecución" in f["proceso"] for f in filas), filas
    print("OK fallo_de_arranque")


def test_deltas_se_suman_y_saldos_no():
    # Tres corridas del mismo cliente: 2+3+1 exitosas, y 4 agotadas que son
    # el mismo saldo repetido, no 12.
    entradas = [
        _entrada(exitosas=2, resultados_por_cliente=[_cliente(exitosas=2, agotadas=4)]),
        _entrada(exitosas=3, resultados_por_cliente=[_cliente(exitosas=3, agotadas=4)]),
        _entrada(exitosas=1, resultados_por_cliente=[_cliente(exitosas=1, agotadas=4)]),
    ]
    consolidado = _consolidar(entradas)
    cli = consolidado["por_tipo"]["facturas"]["clientes"]["Cliente Uno"]

    assert cli["exitosas"] == 6, cli
    assert cli["agotadas"] == 4, f"el saldo no se suma: {cli}"

    mensaje, filas, estado = _construir_reporte(consolidado, "09/08/2026")
    # Una sola fila por cliente y tipo, no una por corrida
    assert sum("Facturas — Cliente Uno" in f["proceso"] for f in filas) == 1, filas
    assert "Sin intentos restantes: 4" in mensaje, mensaje
    assert estado == "Con errores", "documentos atascados deben marcar el reporte"
    print("OK deltas_y_saldos")


def test_cliente_sin_acceso_una_sola_vez():
    cx = {"key_cli": "k1", "cliente": "Cliente Uno", "error": "timeout"}
    entradas = [
        _entrada(etiqueta="facturas", errores_conexion=[cx]),
        _entrada(etiqueta="notas crédito", errores_conexion=[cx]),
        _entrada(etiqueta="documentos de soporte", errores_conexion=[cx]),
    ]
    _, filas, _ = _construir_reporte(_consolidar(entradas), "09/08/2026")

    assert sum("Sin acceso" in f["proceso"] for f in filas) == 1, filas
    print("OK cliente_sin_acceso")


def test_jornada_limpia():
    entradas = [_entrada(exitosas=5, resultados_por_cliente=[_cliente(exitosas=5)])]
    mensaje, filas, estado = _construir_reporte(_consolidar(entradas), "09/08/2026")

    assert estado == "Sin errores", mensaje
    assert all(f["estado"] == "OK" for f in filas), filas
    assert "Total exitosos : 5" in mensaje
    print("OK jornada_limpia")


def _atascado(documento="FE 100", causa="datos", cliente="Cliente Uno", tipo="facturas", **extra):
    d = {"key_cli": "k1", "cliente": cliente, "documento": documento, "tipo": tipo,
         "intentos": 3, "codigo": "NC_NO_SERIAL", "razon": "Missing serial",
         "mensaje": "falta el serial", "causa": causa, "ultimo_intento": "2026-08-09T10:00:00"}
    d.update(extra)
    return d


def test_atascados_no_se_multiplican_por_corrida():
    """El mismo documento atascado aparece en las 18 corridas del día: una fila."""
    ficha = _atascado()
    entradas = [_entrada(atascados=[ficha]) for _ in range(18)]
    consolidado = _consolidar(entradas)

    assert len(consolidado["atascados"]) == 1, consolidado["atascados"]

    # y el reporte diario lo menciona una vez, sin listarlos todos
    mensaje, filas, estado = _construir_reporte(consolidado, "09/08/2026")
    assert estado == "Con errores"
    assert mensaje.count("Sin intentos restantes") == 1, mensaje
    assert sum("sin intentos restantes" in f["proceso"].lower() for f in filas) == 1, filas
    print("OK atascados_no_se_multiplican")


def test_reporte_atascados_agrupa_por_causa():
    atascados = [
        _atascado("FE 100", "datos"),
        _atascado("NC 45", "datos", cliente="Cliente Dos", tipo="notas_credito"),
        _atascado("FE 200", "dian", codigo="137", razon="Error en validaciones Dian"),
        _atascado("DS 7", "tecnica", codigo="HTTP_500", tipo="documentos_soporte"),
    ]
    mensaje, filas, estado = _construir_reporte_atascados(atascados, "09/08/2026")

    assert "4 documento(s)" in estado, estado
    assert len(filas) == 4, filas
    assert all(f["estado"] == "ERROR" for f in filas)

    # Cada causa trae su acción, que es lo que hace útil el reporte
    assert "Completar el dato faltante" in mensaje
    assert "Corregir el documento" in mensaje
    assert "reiniciar el contador" in mensaje

    # Los de datos van primero: son los que alguien puede arreglar hoy
    assert mensaje.index("Faltan datos") < mensaje.index("Rechazados por la DIAN")
    assert "FE 100" in mensaje and "NC 45" in mensaje and "DS 7" in mensaje
    print("OK reporte_atascados")


def test_reporte_atascados_vacio():
    mensaje, filas, estado = _construir_reporte_atascados([], "09/08/2026")
    assert estado == "Sin pendientes" and filas == []
    assert "No hay documentos atascados" in mensaje
    print("OK reporte_atascados_vacio")


if __name__ == "__main__":
    test_fallo_de_arranque_no_es_jornada_limpia()
    test_deltas_se_suman_y_saldos_no()
    test_cliente_sin_acceso_una_sola_vez()
    test_jornada_limpia()
    test_atascados_no_se_multiplican_por_corrida()
    test_reporte_atascados_agrupa_por_causa()
    test_reporte_atascados_vacio()
