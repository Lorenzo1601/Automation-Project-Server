"""Invio opzionale di email (SMTP) per le notifiche. Se ``smtp_host`` non e' configurato non fa nulla."""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from typing import Callable, Optional

from server.config import Settings

log = logging.getLogger("aps.mail")


class Mailer:
    def __init__(self, cfg: Settings, send_fn: Optional[Callable[[EmailMessage], None]] = None, background: bool = True):
        self.cfg, self._send_fn, self.background = cfg, send_fn, background

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.smtp_host) or self._send_fn is not None

    def send(self, to: str, subject: str, body: str) -> None:
        if not self.enabled or not to or "@" not in to:
            return
        msg = EmailMessage()
        msg["From"] = self.cfg.smtp_from or self.cfg.smtp_user or "aps@localhost"
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if self.background:
            threading.Thread(target=self._deliver, args=(msg,), daemon=True).start()
        else:
            self._deliver(msg)

    def _deliver(self, msg: EmailMessage) -> None:
        try:
            if self._send_fn is not None:
                self._send_fn(msg)
                return
            c = self.cfg
            if c.smtp_security == "ssl":
                srv = smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, timeout=15, context=ssl.create_default_context())
            else:
                srv = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=15)
            with srv:
                if c.smtp_security == "starttls":
                    srv.starttls(context=ssl.create_default_context())
                if c.smtp_user:
                    srv.login(c.smtp_user, c.smtp_password)
                srv.send_message(msg)
        except Exception:                                    # noqa: BLE001  (la posta non deve mai bloccare il lavoro)
            log.exception("Invio email a %s non riuscito", msg["To"])
