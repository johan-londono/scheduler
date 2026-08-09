"""Check de api.deps.enmascarar. Ejecutar: python test/test_enmascarar.py"""

import os

os.environ["JWT_SECRET_KEY"] = "clave-de-prueba-" + "x" * 32   # api.auth la exige

from api.deps import enmascarar  # noqa: E402


def test_enmascarar():
    # Claves reales de scheduler_credentials que deben ocultarse
    secretas = [
        "API_SIIGO_PASSWORD", "DOMINUS_CLIENT_SECRET", "DOMINUS_ESUITE_PASSWORD",
        "API_PYTHON_PASSWORD", "MAIN_DB_PASSWORD", "SIIGO_ACCESS_KEY",
        "siigo_access_key", "api_key", "refresh_token",
    ]
    for clave in secretas:
        assert enmascarar({clave: "secreto"}) == {clave: "***"}, f"{clave} quedó expuesta"

    # Identificadores legítimos que deben seguir visibles
    visibles = {
        "key_cli": "abc123", "key_clis": ["a", "b"], "customer_id": 456,
        "API_SIIGO_URL": "https://x", "tipo_doc": "facturas", "MAIN_DB_USER": "postgres",
    }
    assert enmascarar(visibles) == visibles, "enmascaró un valor no sensible"

    # Recursión en dicts anidados
    assert enmascarar({"env": {"DB_PASSWORD": "x", "DB_HOST": "y"}}) == {
        "env": {"DB_PASSWORD": "***", "DB_HOST": "y"}
    }

    assert enmascarar({}) == {}
    print("OK")


if __name__ == "__main__":
    test_enmascarar()
