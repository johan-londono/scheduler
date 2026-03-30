"""
Migración: extrae env_config a una tabla scheduler_credentials y referencia
desde scheduler_tasks via credentials_id (FK).

Resuelve la duplicidad de credenciales compartidas entre tareas (ej: ambas
tareas de Dominus usaban el mismo env_config copiado en cada fila).

Uso:
    python3 scripts/migrar_credentials.py
"""

import json
import os
import sys

from dotenv import dotenv_values

_project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_dir)

from dotenv import load_dotenv
load_dotenv(os.path.join(_project_dir, ".env"))


DDL_CREDENTIALS = """
CREATE TABLE IF NOT EXISTS scheduler_credentials (
    id       SERIAL PRIMARY KEY,
    name     VARCHAR(100) UNIQUE NOT NULL,
    env_vars JSONB NOT NULL DEFAULT '{}'
);
"""

DDL_FK = """
ALTER TABLE scheduler_tasks
    ADD COLUMN IF NOT EXISTS credentials_id INTEGER
        REFERENCES scheduler_credentials(id) ON DELETE SET NULL;
"""

DDL_DROP_ENV_CONFIG = """
ALTER TABLE scheduler_tasks
    DROP COLUMN IF EXISTS env_config;
"""


def leer_env():
    return dotenv_values(os.path.join(_project_dir, ".env"))


def construir_credentials(env):
    """Sets de credenciales a crear, leidos del .env actual."""
    return [
        {
            "name": "siigo_api",
            "env_vars": {
                "API_SIIGO_URL":      env.get("API_SIIGO_URL", ""),
                "API_SIIGO_USER":     env.get("API_SIIGO_USER", ""),
                "API_SIIGO_PASSWORD": env.get("API_SIIGO_PASSWORD", ""),
                "DB_HOST":            env.get("DB_HOST", ""),
                "DB_PORT":            env.get("DB_PORT", "5432"),
                "DB_USER":            env.get("DB_USER", ""),
                "DB_PASSWORD":        env.get("DB_PASSWORD", ""),
                "DB_DATABASE":        "ereports",
            },
        },
        {
            "name": "dominus_api",
            "env_vars": {
                "DOMINUS_API_URL":         env.get("DOMINUS_API_URL", ""),
                "DOMINUS_ESUITE_USER":     env.get("DOMINUS_ESUITE_USER", ""),
                "DOMINUS_ESUITE_PASSWORD": env.get("DOMINUS_ESUITE_PASSWORD", ""),
                "DOMINUS_CLIENT_ID":       env.get("DOMINUS_CLIENT_ID", ""),
                "DOMINUS_CLIENT_SECRET":   env.get("DOMINUS_CLIENT_SECRET", ""),
            },
        },
    ]


# Que credentials_id usar por tarea (None = no necesita credenciales)
CREDENTIALS_POR_TAREA = {
    "sincronizar_siigo_diario":   "siigo_api",
    "sincronizar_dominus_diario": "dominus_api",
    "sincronizar_dominus_mensual": "dominus_api",
    "monitor_apis":               None,
}


def migrar():
    from db import obtener_conexion

    env = leer_env()
    credentials = construir_credentials(env)
    conn = obtener_conexion()

    try:
        with conn.cursor() as cur:
            # 1. Crear tabla de credenciales
            cur.execute(DDL_CREDENTIALS)
            print("Tabla scheduler_credentials creada (o ya existía).")

            # 2. Insertar sets de credenciales
            id_por_nombre = {}
            for cred in credentials:
                cur.execute("""
                    INSERT INTO scheduler_credentials (name, env_vars)
                    VALUES (%s, %s)
                    ON CONFLICT (name) DO UPDATE SET env_vars = EXCLUDED.env_vars
                    RETURNING id
                """, (cred["name"], json.dumps(cred["env_vars"])))
                cred_id = cur.fetchone()[0]
                id_por_nombre[cred["name"]] = cred_id
                print(f"  [+] {cred['name']} (id={cred_id}, {len(cred['env_vars'])} vars)")

            # 3. Agregar columna FK en scheduler_tasks
            cur.execute(DDL_FK)
            print("\nColumna credentials_id agregada a scheduler_tasks.")

            # 4. Asignar FK por tarea
            for nombre_tarea, nombre_cred in CREDENTIALS_POR_TAREA.items():
                cred_id = id_por_nombre.get(nombre_cred) if nombre_cred else None
                cur.execute(
                    "UPDATE scheduler_tasks SET credentials_id = %s WHERE name = %s",
                    (cred_id, nombre_tarea),
                )
                label = nombre_cred if nombre_cred else "NULL"
                print(f"  [~] {nombre_tarea} -> {label}")

            # 5. Eliminar columna env_config (ya reemplazada por FK)
            cur.execute(DDL_DROP_ENV_CONFIG)
            print("\nColumna env_config eliminada.")

        conn.commit()
        print("\nMigración completada.")
        print("Verifica con:")
        print("  SELECT t.name, c.name AS credencial")
        print("  FROM scheduler_tasks t")
        print("  LEFT JOIN scheduler_credentials c ON c.id = t.credentials_id;")
    except Exception as e:
        conn.rollback()
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    migrar()
