import os
import sys

from dotenv import load_dotenv
from fastapi import FastAPI

# Asegurar que la raiz del proyecto este en sys.path
_project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_project_dir, ".env"))
if _project_dir not in sys.path:
    sys.path.insert(0, _project_dir)

from api.routers import auth, tasks, credentials, system

app = FastAPI(
    title="Scheduler API",
    description="API para gestionar tareas programadas y credenciales del scheduler.",
    version="1.0.0",
)

app.include_router(auth.router,        prefix="/auth",        tags=["auth"])
app.include_router(tasks.router,       prefix="/tasks",       tags=["tasks"])
app.include_router(credentials.router, prefix="/credentials", tags=["credentials"])
app.include_router(system.router,      prefix="/system",      tags=["system"])
