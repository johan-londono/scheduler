from typing import Callable

import jwt
import psycopg2.extras
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.auth import decodificar_access_token
from db import obtener_conexion

_bearer = HTTPBearer()


def get_db():
    """Dependency de FastAPI: abre una conexión por request y la cierra al terminar."""
    conn = obtener_conexion()
    try:
        yield conn
    finally:
        conn.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    conn=Depends(get_db),
) -> dict:
    """Valida el JWT y retorna el usuario activo.

    Punto de extensión: para agregar API Keys u OAuth2, modificar solo esta función.
    Los routers no necesitan cambios.
    """
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido o expirado.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decodificar_access_token(credentials.credentials)
    except jwt.InvalidTokenError:
        raise exc

    user_id = int(payload["sub"])
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, email, rol, activo FROM scheduler_users WHERE id = %s",
            (user_id,),
        )
        user = cur.fetchone()

    if not user or not user["activo"]:
        raise exc

    return dict(user)


# Jerarquía de roles: admin > operator > viewer
_ROL_NIVEL = {"viewer": 0, "operator": 1, "admin": 2}


def require_role(*roles: str) -> Callable:
    """Factory de dependencias por rol. Acepta el rol requerido o superiores."""
    nivel_requerido = min(_ROL_NIVEL[r] for r in roles)

    def _check(user: dict = Depends(get_current_user)):
        if _ROL_NIVEL.get(user["rol"], -1) < nivel_requerido:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para realizar esta acción.",
            )
        return user

    return _check
