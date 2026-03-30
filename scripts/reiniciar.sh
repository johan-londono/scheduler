#!/bin/bash
# Reinicia celery-worker, celery-beat y celery-api.
# Necesario despues de cambios en la tabla scheduler_tasks, .env, tasks/ o app.py.
# Uso: sudo bash scripts/reiniciar.sh

set -e

echo "Reiniciando servicios Celery..."
sudo systemctl daemon-reload
sudo systemctl restart celery-worker celery-beat celery-api

echo ""
sudo systemctl status celery-worker celery-beat celery-api --no-pager
