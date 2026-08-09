"""Lanzador único de los subprocesos de tareas (ETL Dominus, reenvío DIAN).

Cada tarea buscaba el intérprete a su manera y las dos listas de candidatos ya
habían divergido. Todos los entry points se ejecutan igual: `python -m modulo`
desde la raíz del proyecto, así que no hace falta calcular rutas de archivo ni
jugar con el cwd.
"""
import os
import subprocess

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def interprete() -> str:
    """Ruta del python a usar: el del venv del proyecto si existe."""
    for candidato in (
        os.path.join(RAIZ, ".venv", "bin", "python3"),
        os.path.join(RAIZ, "venv", "bin", "python3"),
        "/usr/bin/python3",
    ):
        if os.path.isfile(candidato):
            return candidato
    return "python3"


def correr_modulo(modulo: str, args: list, env_config: dict = None, timeout: int = 900):
    """Ejecuta `python -m <modulo> <args>` y devuelve el CompletedProcess.

    env_config son los overrides que vienen de scheduler_credentials; el resto
    del entorno del worker se hereda.
    """
    env = os.environ.copy()
    if env_config:
        env.update({k: str(v) for k, v in env_config.items()})

    return subprocess.run(
        [interprete(), "-m", modulo, *args],
        capture_output=True,
        text=True,
        cwd=RAIZ,
        timeout=timeout,
        env=env,
    )
