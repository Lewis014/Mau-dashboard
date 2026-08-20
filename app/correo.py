"""
Envio de correo por SMTP. Esto es solo el transporte: QUE se manda lo decide app/alertas.py.

smtplib de la biblioteca estandar, igual que el resto del proyecto evita dependencias nuevas
(urllib en vez de httpx en sync_planes y en el webhook de alertas).

La configuracion son las MISMAS variables que ya usa mau-web (MAIL_HOST, MAIL_PORT,
MAIL_USERNAME, MAIL_PASSWORD, MAIL_ENCRYPTION). Reutilizar esa cuenta y esos nombres evita
tener dos remitentes distintos y dos sitios donde mirar cuando un correo no llega.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

MAIL_HOST = os.getenv("MAIL_HOST", "").strip()
MAIL_PORT = int(os.getenv("MAIL_PORT", "465"))
MAIL_USERNAME = os.getenv("MAIL_USERNAME", "").strip()
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
MAIL_ENCRYPTION = os.getenv("MAIL_ENCRYPTION", "ssl").strip().lower()
# Por defecto se envia desde la propia cuenta autenticada: muchos servidores rechazan un
# From que no coincida con el usuario, y es un fallo dificil de leer en los logs.
MAIL_FROM = os.getenv("MAIL_FROM_ADDRESS", "").strip() or MAIL_USERNAME
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "MAU · Contatech")

TIMEOUT = 30


def configurado() -> bool:
    return bool(MAIL_HOST and MAIL_USERNAME and MAIL_PASSWORD and MAIL_FROM)


def estado() -> str:
    """Resumen legible para el log de arranque. Nunca incluye la contrasena."""
    if not configurado():
        faltan = [k for k, v in (("MAIL_HOST", MAIL_HOST), ("MAIL_USERNAME", MAIL_USERNAME),
                                 ("MAIL_PASSWORD", MAIL_PASSWORD)) if not v]
        return f"sin configurar (falta {', '.join(faltan) or 'MAIL_FROM_ADDRESS'})"
    return f"{MAIL_FROM} vía {MAIL_HOST}:{MAIL_PORT} ({MAIL_ENCRYPTION or 'sin cifrar'})"


def enviar(destinatarios: list[str], asunto: str, texto: str, html: Optional[str] = None) -> None:
    """Manda un correo. BLOQUEANTE: llamarlo siempre desde asyncio.to_thread.

    Va en texto plano y HTML a la vez (multipart/alternative): el cliente elige. El texto
    plano no es relleno — es lo que se ve en la previsualizacion del movil y en los clientes
    que bloquean HTML, que es justo cuando alguien mira el correo de reojo.
    """
    if not configurado():
        raise RuntimeError("Correo sin configurar: " + estado())
    if not destinatarios:
        raise ValueError("Sin destinatarios")

    msg = EmailMessage()
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM))
    msg["To"] = ", ".join(destinatarios)
    msg["Subject"] = asunto
    msg.set_content(texto)
    if html:
        msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    if MAIL_ENCRYPTION == "ssl":
        with smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, context=ctx, timeout=TIMEOUT) as s:
            s.login(MAIL_USERNAME, MAIL_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=TIMEOUT) as s:
            if MAIL_ENCRYPTION in ("tls", "starttls"):
                s.starttls(context=ctx)
            s.login(MAIL_USERNAME, MAIL_PASSWORD)
            s.send_message(msg)
