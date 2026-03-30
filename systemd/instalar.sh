#!/bin/bash
# Copia los archivos .service al directorio de systemd y habilita los servicios.
# Ejecutar como root desde la raiz del proyecto:
#   sudo bash systemd/instalar.sh

set -e

SYSTEMD_DIR="/etc/systemd/system"

echo "Copiando archivos de servicio..."
cp systemd/celery-worker.service "$SYSTEMD_DIR/celery-worker.service"
cp systemd/celery-beat.service   "$SYSTEMD_DIR/celery-beat.service"
cp systemd/celery-api.service    "$SYSTEMD_DIR/celery-api.service"

echo "Recargando systemd..."
systemctl daemon-reload

echo "Habilitando servicios para inicio automatico..."
systemctl enable celery-worker celery-beat celery-api

echo "Iniciando servicios..."
systemctl start celery-worker celery-beat celery-api

echo ""
systemctl status celery-worker celery-beat celery-api --no-pager
