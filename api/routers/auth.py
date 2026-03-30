"""Endpoints de autenticación: login, refresh de token, logout."""
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from api.auth import (
    crear_access_token,
    crear_refresh_token,
    hash_refresh_token,
    verificar_password,
)
from api.deps import get_db

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/login")
def login(body: LoginRequest, conn=Depends(get_db)):
    """Valida credenciales y retorna access_token + refresh_token."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, email, password_hash, rol, activo FROM scheduler_users WHERE email = %s",
            (body.email,),
        )
        user = cur.fetchone()

    if not user or not user["activo"] or not verificar_password(body.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas.",
        )

    access_token = crear_access_token(user["id"], user["email"], user["rol"])
    refresh_token_str, expira_en = crear_refresh_token()
    token_hash = hash_refresh_token(refresh_token_str)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO scheduler_refresh_tokens (user_id, token_hash, expira_en)
            VALUES (%s, %s, %s)
        """, (user["id"], token_hash, expira_en))
    conn.commit()

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token_str,
    }


@router.post("/refresh")
def refresh(body: RefreshRequest, conn=Depends(get_db)):
    """Rota el refresh token: invalida el actual y emite uno nuevo.

    Si el token ya estaba revocado → invalida TODOS los tokens del usuario
    (detección de robo de token).
    """
    token_hash = hash_refresh_token(body.refresh_token)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT rt.id, rt.user_id, rt.revocado, rt.expira_en,
                   u.email, u.rol, u.activo
            FROM scheduler_refresh_tokens rt
            JOIN scheduler_users u ON u.id = rt.user_id
            WHERE rt.token_hash = %s
        """, (token_hash,))
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido.")

    if row["revocado"]:
        # Posible robo — invalidar todos los tokens del usuario
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE scheduler_refresh_tokens SET revocado = TRUE WHERE user_id = %s",
                (row["user_id"],),
            )
        conn.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token ya utilizado. Todas las sesiones han sido cerradas por seguridad.",
        )

    from datetime import datetime, timezone
    if row["expira_en"].replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expirado.")

    if not row["activo"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario inactivo.")

    # Revocar el token actual
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scheduler_refresh_tokens SET revocado = TRUE WHERE id = %s",
            (row["id"],),
        )

    # Emitir nuevos tokens
    access_token = crear_access_token(row["user_id"], row["email"], row["rol"])
    nuevo_refresh_str, expira_en = crear_refresh_token()
    nuevo_hash = hash_refresh_token(nuevo_refresh_str)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO scheduler_refresh_tokens (user_id, token_hash, expira_en)
            VALUES (%s, %s, %s)
        """, (row["user_id"], nuevo_hash, expira_en))
    conn.commit()

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": nuevo_refresh_str,
    }


@router.post("/logout", status_code=204)
def logout(body: RefreshRequest, conn=Depends(get_db)):
    """Revoca el refresh token. Siempre retorna 204 (no revela si existía)."""
    token_hash = hash_refresh_token(body.refresh_token)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scheduler_refresh_tokens SET revocado = TRUE WHERE token_hash = %s",
            (token_hash,),
        )
    conn.commit()
