"""Utilidades de autenticación JWT y hashing de contraseñas."""
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

# Sin default: PyJWT firma HS256 con clave vacía sin protestar, así que un .env
# incompleto dejaba la API aceptando tokens que cualquiera puede fabricar.
_SECRET_KEY = os.environ.get("JWT_SECRET_KEY") or ""
if len(_SECRET_KEY) < 32:
    raise RuntimeError(
        "JWT_SECRET_KEY falta o es demasiado corta (mínimo 32 caracteres). "
        "Generar con: python3 -c \"import secrets; print(secrets.token_hex(64))\""
    )

_ALGORITHM = "HS256"
_ACCESS_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
_REFRESH_EXPIRE_DAYS = int(os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS", "7"))


# ── Contraseñas ───────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verificar_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── Access token (JWT) ────────────────────────────────────────────────────────

def crear_access_token(user_id: int, email: str, rol: str) -> str:
    expira = datetime.now(timezone.utc) + timedelta(minutes=_ACCESS_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "email": email,
        "rol": rol,
        "type": "access",
        "exp": expira,
    }
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def decodificar_access_token(token: str) -> dict:
    """Decodifica y valida el JWT. Lanza jwt.InvalidTokenError si es inválido."""
    payload = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Tipo de token incorrecto")
    return payload


# ── Refresh token (opaco) ─────────────────────────────────────────────────────

def crear_refresh_token() -> tuple[str, datetime]:
    """Retorna (token_str, expira_en). El token es un string opaco — nunca guardarlo en claro."""
    token = secrets.token_urlsafe(48)
    expira = datetime.now(timezone.utc) + timedelta(days=_REFRESH_EXPIRE_DAYS)
    return token, expira


def hash_refresh_token(token: str) -> str:
    """SHA-256 del token para guardar en DB."""
    return hashlib.sha256(token.encode()).hexdigest()
