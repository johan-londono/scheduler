import os
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage

import redis

from app import celery_app

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _cargar_plantilla(nombre, asunto, mensaje, ahora):
    """Carga templates/nombre.html e interpola {asunto}, {mensaje}, {ahora}."""
    ruta = os.path.join(_TEMPLATES_DIR, f"{nombre}.html")
    if not os.path.isfile(ruta):
        raise FileNotFoundError(f"Plantilla no encontrada: {ruta}")
    with open(ruta, "r", encoding="utf-8") as f:
        return f.read().format(asunto=asunto, mensaje=mensaje, ahora=ahora)


def _obtener_numero_correo():
    """Obtiene un consecutivo para correos de prueba usando Redis."""
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        cliente = redis.Redis.from_url(redis_url)
        return cliente.incr("scheduler:correo_prueba:contador")
    except Exception as error:
        logger.warning(f"No se pudo obtener contador en Redis: {error}")
        return int(datetime.now().timestamp())


def _construir_html(asunto, mensaje, datos_reporte, ahora):
    """Construye el contenido HTML del correo."""
    filas_html = ""

    if datos_reporte and datos_reporte.get("resultados"):
        for r in datos_reporte["resultados"]:
            es_ok = r["estado"] == "OK"
            color = "#16a34a" if es_ok else "#dc2626"
            icono = "&#10003;" if es_ok else "&#10007;"
            badge_bg = "#dcfce7" if es_ok else "#fee2e2"
            detalle = f'<br><span style="color:#6b7280;font-size:12px">{r["detalle"]}</span>' if r["detalle"] else ""

            filas_html += f"""
            <tr>
                <td style="padding:12px 16px;border-bottom:1px solid #f3f4f6;font-size:14px;color:#374151">{r["proceso"]}</td>
                <td style="padding:12px 16px;border-bottom:1px solid #f3f4f6;text-align:center">
                    <span style="display:inline-block;padding:4px 12px;border-radius:12px;font-size:13px;font-weight:600;color:{color};background:{badge_bg}">{icono} {r["estado"]}</span>
                    {detalle}
                </td>
            </tr>"""

        filas_resumen = ""
        if datos_reporte.get('customer_id'):
            filas_resumen += f"""
            <tr>
                <td style="color:#6b7280;font-size:13px">Customer ID</td>
                <td style="text-align:right;font-weight:600;font-size:13px;color:#111827">{datos_reporte['customer_id']}</td>
            </tr>"""
        if datos_reporte.get('fecha_inicio') and datos_reporte.get('fecha_fin'):
            filas_resumen += f"""
            <tr>
                <td style="color:#6b7280;font-size:13px">Periodo</td>
                <td style="text-align:right;font-weight:600;font-size:13px;color:#111827">{datos_reporte['fecha_inicio']} &rarr; {datos_reporte['fecha_fin']}</td>
            </tr>"""
        resumen_info = f'<table style="width:100%;margin-bottom:8px">{filas_resumen}</table>' if filas_resumen else ""

        total = len(datos_reporte["resultados"])
        exitosos = sum(1 for r in datos_reporte["resultados"] if r["estado"] == "OK")
        fallidos = total - exitosos
        estado_general = "Completado" if fallidos == 0 else f"{fallidos} error(es)"
        color_general = "#16a34a" if fallidos == 0 else "#dc2626"

        stats_html = f"""
        <table style="width:100%;margin-top:16px;margin-bottom:16px">
            <tr>
                <td style="text-align:center;padding:12px">
                    <div style="font-size:24px;font-weight:700;color:#111827">{total}</div>
                    <div style="font-size:12px;color:#6b7280">Procesos</div>
                </td>
                <td style="text-align:center;padding:12px">
                    <div style="font-size:24px;font-weight:700;color:#16a34a">{exitosos}</div>
                    <div style="font-size:12px;color:#6b7280">Exitosos</div>
                </td>
                <td style="text-align:center;padding:12px">
                    <div style="font-size:24px;font-weight:700;color:#dc2626">{fallidos}</div>
                    <div style="font-size:12px;color:#6b7280">Fallidos</div>
                </td>
            </tr>
        </table>"""
    else:
        resumen_info = ""
        stats_html = ""
        estado_general = "Info"
        color_general = "#2563eb"

    cuerpo_tabla = f"""
        <table style="width:100%;border-collapse:collapse;margin-top:8px">
            <thead>
                <tr style="background:#f9fafb">
                    <th style="padding:10px 16px;text-align:left;font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #e5e7eb">Proceso</th>
                    <th style="padding:10px 16px;text-align:center;font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #e5e7eb">Estado</th>
                </tr>
            </thead>
            <tbody>{filas_html}</tbody>
        </table>""" if filas_html else f'<p style="color:#374151;font-size:14px;line-height:1.6;white-space:pre-wrap">{mensaje}</p>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
    <table style="width:100%;max-width:600px;margin:32px auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
        <!-- Header -->
        <tr>
            <td style="background:linear-gradient(135deg,#1e3a5f 0%,#2563eb 100%);padding:28px 32px">
                <h1 style="margin:0;color:#ffffff;font-size:20px;font-weight:600">{asunto}</h1>
                <p style="margin:6px 0 0;color:#93c5fd;font-size:13px">{ahora}</p>
            </td>
        </tr>

        <!-- Estado general -->
        <tr>
            <td style="padding:20px 32px 0">
                <span style="display:inline-block;padding:4px 14px;border-radius:12px;font-size:13px;font-weight:600;color:{color_general};background:{'#dcfce7' if color_general == '#16a34a' else '#fee2e2' if color_general == '#dc2626' else '#dbeafe'}">{estado_general}</span>
            </td>
        </tr>

        <!-- Resumen -->
        <tr>
            <td style="padding:16px 32px 0">{resumen_info}</td>
        </tr>

        <!-- Stats -->
        <tr>
            <td style="padding:0 32px">{stats_html}</td>
        </tr>

        <!-- Tabla de resultados -->
        <tr>
            <td style="padding:0 32px 24px">{cuerpo_tabla}</td>
        </tr>

        <!-- Footer -->
        <tr>
            <td style="padding:16px 32px;background:#f9fafb;border-top:1px solid #e5e7eb">
                <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center">Scheduler &mdash; eHolding</p>
            </td>
        </tr>
    </table>
</body>
</html>"""


@celery_app.task(name="tasks.correo.enviar_correo")
def enviar_correo(asunto="Correo programado", mensaje="Mensaje automático del scheduler", destinatarios=None, datos_reporte=None, plantilla=None):
    """Envía un correo HTML usando configuración MAIL_* o SMTP_* por variables de entorno."""
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mailer = (os.environ.get("MAIL_MAILER") or "smtp").lower()
    smtp_host = os.environ.get("SMTP_HOST") or os.environ.get("MAIL_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT") or os.environ.get("MAIL_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER") or os.environ.get("MAIL_USERNAME")
    smtp_password = os.environ.get("SMTP_PASSWORD") or os.environ.get("MAIL_PASSWORD")
    remitente = os.environ.get("SMTP_FROM") or os.environ.get("MAIL_FROM_ADDRESS") or smtp_user
    encryption = (os.environ.get("MAIL_ENCRYPTION") or "tls").lower()
    usar_tls = os.environ.get("SMTP_USE_TLS", "true").lower() == "true" and encryption in ("tls", "starttls", "")

    if mailer != "smtp":
        logger.error(f"[{ahora}] MAIL_MAILER={mailer} no es soportado. Usa smtp.")
        return "Error: MAIL_MAILER no soportado"

    if destinatarios is None:
        destino_env = os.environ.get("SMTP_TO") or os.environ.get("MAIL_TO", "")
        destinatarios = [correo.strip() for correo in destino_env.split(",") if correo.strip()]

    if not smtp_host or not smtp_user or not smtp_password or not remitente or not destinatarios:
        logger.error(
            "[%s] No se pudo enviar correo: falta configuración (MAIL_HOST/SMTP_HOST, MAIL_USERNAME/SMTP_USER, MAIL_PASSWORD/SMTP_PASSWORD, MAIL_FROM_ADDRESS/SMTP_FROM, MAIL_TO/SMTP_TO).",
            ahora,
        )
        return "Error: configuración de correo incompleta"

    email = EmailMessage()
    email["Subject"] = asunto
    email["From"] = remitente
    email["To"] = ", ".join(destinatarios)

    # Texto plano siempre presente como fallback
    email.set_content(mensaje)

    # Contenido HTML segun plantilla elegida
    if plantilla == "plain":
        # Sin HTML: solo texto plano
        pass
    elif plantilla is None or plantilla == "default":
        html = _construir_html(asunto, mensaje, datos_reporte, ahora)
        email.add_alternative(html, subtype="html")
    else:
        try:
            html = _cargar_plantilla(plantilla, asunto, mensaje, ahora)
            email.add_alternative(html, subtype="html")
        except FileNotFoundError as e:
            logger.error(f"[{ahora}] {e}. Se enviará solo texto plano.")

    logger.info(f"[{ahora}] Enviando correo a {', '.join(destinatarios)}...")
    with smtplib.SMTP(smtp_host, smtp_port) as servidor:
        if usar_tls:
            servidor.starttls()
        servidor.login(smtp_user, smtp_password)
        servidor.send_message(email)

    logger.info("Correo enviado exitosamente.")
    return f"Correo enviado a {', '.join(destinatarios)} a las {ahora}"
