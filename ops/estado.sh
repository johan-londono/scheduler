#!/bin/bash
# Muestra el estado de los servicios y las tareas registradas en beat_schedule.
# Uso: bash ops/estado.sh

echo "=== Estado de servicios ==="
systemctl status celery-worker celery-beat celery-api --no-pager

echo ""
echo "=== Tareas activas en la DB ==="
cd "$(dirname "$0")/.."
.venv/bin/python3 -c "
from app import construir_schedule
for nombre, conf in construir_schedule().items():
    print(f'  {nombre}: {conf[\"task\"]}')
"
