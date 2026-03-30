#!/bin/bash
# Detiene celery-worker y celery-beat.
# Uso: sudo bash scripts/detener.sh

echo "Deteniendo servicios Celery..."
sudo systemctl stop celery-worker celery-beat

echo "Servicios detenidos."
sudo systemctl status celery-worker celery-beat --no-pager
