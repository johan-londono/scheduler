import os
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage

import redis

from app import celery_app

logger = logging.getLogger(__name__)


def _obtener_numero_correo():
    """Obtiene un consecutivo para correos de prueba usando Redis."""
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        cliente = redis.Redis.from_url(redis_url)
        return cliente.incr("scheduler:correo_prueba:contador")
    except Exception as error:
        logger.warning(f"No se pudo obtener contador en Redis: {error}")
        return int(datetime.now().timestamp())


@celery_app.task(name="tasks.correo.enviar_correo")
def enviar_correo(asunto="Correo programado", mensaje="Mensaje automático del scheduler", destinatarios=None):
    """Envía un correo usando configuración MAIL_* o SMTP_* por variables de entorno."""
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

    numero_correo = _obtener_numero_correo()
    asunto_final = f"{asunto} #{numero_correo}"
    mensaje_final = f"{mensaje}\n\nNúmero de correo: {numero_correo}"

    email = EmailMessage()
    email["Subject"] = asunto_final
    email["From"] = remitente
    email["To"] = ", ".join(destinatarios)
    email.set_content(mensaje_final)

    logger.info(f"[{ahora}] Enviando correo a {', '.join(destinatarios)}...")
    with smtplib.SMTP(smtp_host, smtp_port) as servidor:
        if usar_tls:
            servidor.starttls()
        servidor.login(smtp_user, smtp_password)
        servidor.send_message(email)

    logger.info("Correo enviado exitosamente.")
    return f"Correo #{numero_correo} enviado a {', '.join(destinatarios)} a las {ahora}"
