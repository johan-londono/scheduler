#!/bin/bash
# Reinicia celery-worker y celery-beat.
# Necesario despues de cambios en la tabla scheduler_tasks, .env, tasks/ o app.py.
# Uso: sudo bash scripts/reiniciar.sh

set -e

echo "Reiniciando servicios Celery..."
sudo systemctl daemon-reload
sudo systemctl restart celery-worker celery-beat

echo ""
sudo systemctl status celery-worker celery-beat --no-pager
