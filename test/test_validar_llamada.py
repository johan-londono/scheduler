"""Check de la validación function/kwargs de la API. Ejecutar: python test/test_validar_llamada.py"""

import os

os.environ["JWT_SECRET_KEY"] = "clave-de-prueba-" + "x" * 32   # api.auth la exige

from fastapi import HTTPException  # noqa: E402

from api.routers.tasks import _validar_llamada  # noqa: E402


def _falla(function, args=None, kwargs=None, credentials_id=None) -> str:
    try:
        _validar_llamada(function, args or [], kwargs or {}, credentials_id)
    except HTTPException as exc:
        return exc.detail
    return ""


def test_validar_llamada():
    # Llamadas válidas: no levantan nada
    _validar_llamada("tasks.siigo.sincronizar_siigo", [], {"customer_id": 23}, 1)
    _validar_llamada("tasks.correo.enviar_correo", [], {"asunto": "x", "plantilla": "plain"}, None)
    _validar_llamada("tasks.dominus.sincronizar_dominus", [], {"branch_id": 1054}, 2)

    # Función inexistente (el typo clásico del README viejo)
    assert "no está registrada" in _falla("tasks.envio_correo.enviar_correo")

    # plantilla sí se acepta ahora: las tareas Dominus de la DB la traen
    _validar_llamada("tasks.dominus.sincronizar_dominus", [], {"plantilla": "simple"}, 2)

    # kwarg que la función no acepta
    assert "no encajan" in _falla("tasks.dominus.sincronizar_dominus", kwargs={"sucursal": 1})

    # Falta un parámetro obligatorio — caso real de sincronizar_siigo_mes_anterior
    assert "no encajan" in _falla("tasks.siigo.sincronizar_siigo", kwargs={"mes_anterior": True})

    # Credenciales asignadas a una función que no acepta env_config
    assert "no encajan" in _falla("tasks.correo.enviar_correo", credentials_id=3)

    # **_ absorbe cualquier kwarg: no debe rechazarse
    _validar_llamada("tasks.reenvio_dian.reenviar_facturas_dian", [], {"lo_que_sea": 1}, 3)

    print("OK validar_llamada")


if __name__ == "__main__":
    test_validar_llamada()
