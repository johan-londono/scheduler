#!/bin/bash
# Detiene celery-worker, celery-beat y celery-api.
# Uso: sudo bash ops/detener.sh

echo "Deteniendo servicios Celery..."
sudo systemctl stop celery-worker celery-beat celery-api

echo "Servicios detenidos."
sudo systemctl status celery-worker celery-beat celery-api --no-pager
