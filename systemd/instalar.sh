#!/bin/bash
# Sustituye los placeholders en los archivos .service y los instala en systemd.
#
# Uso (desde la raíz del proyecto):
#   sudo bash systemd/instalar.sh --path /ruta/al/proyecto --user www-data
#
# Parámetros:
#   --path   Ruta absoluta donde vive el proyecto   (reemplaza DEPLOY_PATH)
#   --user   Usuario y grupo del proceso            (reemplaza DEPLOY_USER / DEPLOY_GROUP)

set -e

DEPLOY_PATH=""
DEPLOY_USER=""

# ── Parsear argumentos ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --path)  DEPLOY_PATH="$2"; shift 2 ;;
        --user)  DEPLOY_USER="$2"; shift 2 ;;
        *) echo "Parámetro desconocido: $1"; exit 1 ;;
    esac
done

if [[ -z "$DEPLOY_PATH" || -z "$DEPLOY_USER" ]]; then
    echo "Uso: sudo bash systemd/instalar.sh --path /ruta/proyecto --user nombre_usuario"
    exit 1
fi

SYSTEMD_DIR="/etc/systemd/system"
SERVICES=(celery-worker celery-beat celery-api)

echo "Instalando servicios con:"
echo "  DEPLOY_PATH  = $DEPLOY_PATH"
echo "  DEPLOY_USER  = $DEPLOY_USER"
echo ""

for svc in "${SERVICES[@]}"; do
    src="systemd/${svc}.service"
    dst="${SYSTEMD_DIR}/${svc}.service"

    sed \
        -e "s|DEPLOY_PATH|${DEPLOY_PATH}|g" \
        -e "s|DEPLOY_USER|${DEPLOY_USER}|g" \
        -e "s|DEPLOY_GROUP|${DEPLOY_USER}|g" \
        "$src" > "$dst"

    echo "  → $dst"
done

echo ""
echo "Recargando systemd..."
systemctl daemon-reload

echo "Habilitando servicios para inicio automático..."
systemctl enable "${SERVICES[@]}"

echo "Iniciando servicios..."
systemctl start "${SERVICES[@]}"

echo ""
systemctl status "${SERVICES[@]}" --no-pager
