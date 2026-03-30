#!/bin/bash
# Muestra el estado de los servicios y las tareas registradas en beat_schedule.
# Uso: bash scripts/estado.sh

echo "=== Estado de servicios ==="
systemctl status celery-worker celery-beat --no-pager

echo ""
echo "=== Tareas registradas en beat_schedule ==="
cd "$(dirname "$0")/.."
.venv/bin/python3 -c "
from app import celery_app
for nombre, conf in celery_app.conf.beat_schedule.items():
    print(f'  {nombre}: {conf[\"task\"]}')
"
