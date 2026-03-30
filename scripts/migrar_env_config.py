"""
Migración: reemplaza la columna db_config por env_config en scheduler_tasks.

env_config es un JSONB donde cada clave es el nombre exacto de la variable de
entorno que el subprocess necesita. Al ejecutarse una tarea, esos valores
sobreescriben las variables del entorno del proceso padre.

Esta migración lee el .env actual para poblar las credenciales iniciales de
cada tarea. Despues de ejecutarla, las credenciales viven en la DB y se pueden
actualizar directamente en la tabla sin tocar el .env del servidor.

Uso:
    python3 scripts/migrar_env_config.py
"""

import json
import os
import sys

from dotenv import load_dotenv, dotenv_values

_project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_project_dir, ".env"))
sys.path.insert(0, _project_dir)


def leer_env():
    """Lee el .env como dict (sin pisar os.environ)."""
    return dotenv_values(os.path.join(_project_dir, ".env"))


def construir_env_config(env):
    """Retorna el env_config inicial por tarea leyendo el .env actual."""
    return {
        "sincronizar_siigo_diario": {
            "API_SIIGO_URL":      env.get("API_SIIGO_URL", ""),
            "API_SIIGO_USER":     env.get("API_SIIGO_USER", ""),
            "API_SIIGO_PASSWORD": env.get("API_SIIGO_PASSWORD", ""),
            "DB_HOST":            env.get("DB_HOST", ""),
            "DB_PORT":            env.get("DB_PORT", "5432"),
            "DB_USER":            env.get("DB_USER", ""),
            "DB_PASSWORD":        env.get("DB_PASSWORD", ""),
            "DB_DATABASE":        "ereports",
        },
        "sincronizar_dominus_diario": {
            "DOMINUS_API_URL":        env.get("DOMINUS_API_URL", ""),
            "DOMINUS_ESUITE_USER":    env.get("DOMINUS_ESUITE_USER", ""),
            "DOMINUS_ESUITE_PASSWORD": env.get("DOMINUS_ESUITE_PASSWORD", ""),
            "DOMINUS_CLIENT_ID":      env.get("DOMINUS_CLIENT_ID", ""),
            "DOMINUS_CLIENT_SECRET":  env.get("DOMINUS_CLIENT_SECRET", ""),
        },
        "sincronizar_dominus_mensual": {
            "DOMINUS_API_URL":        env.get("DOMINUS_API_URL", ""),
            "DOMINUS_ESUITE_USER":    env.get("DOMINUS_ESUITE_USER", ""),
            "DOMINUS_ESUITE_PASSWORD": env.get("DOMINUS_ESUITE_PASSWORD", ""),
            "DOMINUS_CLIENT_ID":      env.get("DOMINUS_CLIENT_ID", ""),
            "DOMINUS_CLIENT_SECRET":  env.get("DOMINUS_CLIENT_SECRET", ""),
        },
        "monitor_apis": None,
    }


def migrar():
    from db import obtener_conexion

    env = leer_env()
    env_configs = construir_env_config(env)
    conn = obtener_conexion()

    try:
        with conn.cursor() as cur:
            # 1. Agregar columna env_config si no existe
            cur.execute("""
                ALTER TABLE scheduler_tasks
                ADD COLUMN IF NOT EXISTS env_config JSONB NULL;
            """)
            print("Columna env_config agregada (o ya existia).")

            # 2. Poblar env_config por tarea
            for nombre, config in env_configs.items():
                cur.execute(
                    "UPDATE scheduler_tasks SET env_config = %s WHERE name = %s",
                    (json.dumps(config) if config else None, nombre),
                )
                filas = cur.rowcount
                if filas:
                    label = f"{len(config)} vars" if config else "NULL"
                    print(f"  [~] {nombre}: env_config = {label}")
                else:
                    print(f"  [?] {nombre}: no encontrada en la tabla")

            # 3. Eliminar columna db_config si existe
            cur.execute("""
                ALTER TABLE scheduler_tasks
                DROP COLUMN IF EXISTS db_config;
            """)
            print("Columna db_config eliminada (o no existia).")

        conn.commit()
        print("\nMigracion completada.")
        print("Verifica las credenciales con:")
        print("  SELECT name, env_config FROM scheduler_tasks;")
    except Exception as e:
        conn.rollback()
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    migrar()
