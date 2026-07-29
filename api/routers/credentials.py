import json

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import enmascarar, get_db, require_role

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class CredentialCreate(BaseModel):
    name: str
    env_vars: dict[str, str]


class CredentialPatch(BaseModel):
    env_vars: dict[str, str]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_credentials(conn=Depends(get_db), _=Depends(require_role("viewer"))):
    """Lista todos los sets de credenciales (valores sensibles enmascarados)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT c.id, c.name, c.env_vars,
                   ARRAY_AGG(t.name) FILTER (WHERE t.name IS NOT NULL) AS tasks
            FROM scheduler_credentials c
            LEFT JOIN scheduler_tasks t ON t.credentials_id = c.id
            GROUP BY c.id
            ORDER BY c.id
        """)
        rows = cur.fetchall()

    return [
        {**row, "env_vars": enmascarar(dict(row["env_vars"] or {}))}
        for row in rows
    ]


@router.post("", status_code=201)
def create_credential(body: CredentialCreate, conn=Depends(get_db), _=Depends(require_role("admin"))):
    """Crea un nuevo set de credenciales."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        try:
            cur.execute(
                "INSERT INTO scheduler_credentials (name, env_vars) VALUES (%s, %s) RETURNING id, name",
                (body.name, json.dumps(body.env_vars)),
            )
            conn.commit()
            row = cur.fetchone()
            return {"id": row["id"], "name": row["name"], "env_vars": enmascarar(body.env_vars)}
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{credential_id}")
def update_credential(credential_id: int, body: CredentialPatch, conn=Depends(get_db), _=Depends(require_role("admin"))):
    """Fusiona env_vars con los valores existentes (no reemplaza el set completo)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "UPDATE scheduler_credentials SET env_vars = env_vars || %s WHERE id = %s RETURNING id, name, env_vars",
            (json.dumps(body.env_vars), credential_id),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"Credencial id={credential_id} no encontrada.")
        conn.commit()
        return {**row, "env_vars": enmascarar(dict(row["env_vars"]))}


@router.delete("/{credential_id}", status_code=204)
def delete_credential(credential_id: int, conn=Depends(get_db), _=Depends(require_role("admin"))):
    """Elimina un set de credenciales. Falla si alguna tarea lo referencia."""
    with conn.cursor() as cur:
        try:
            cur.execute("DELETE FROM scheduler_credentials WHERE id = %s", (credential_id,))
            if cur.rowcount == 0:
                conn.rollback()
                raise HTTPException(status_code=404, detail=f"Credencial id={credential_id} no encontrada.")
            conn.commit()
        except HTTPException:
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(e))
