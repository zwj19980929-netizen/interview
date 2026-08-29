from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol


@dataclass(frozen=True)
class OutboundEmail:
    recipient: str
    subject: str
    text_body: str
    message_id: str


class EmailSender(Protocol):
    @property
    def configured(self) -> bool: ...

    def send(self, message: OutboundEmail) -> None: ...


class SmtpEmailSender:
    """Small SMTP adapter; credentials are read only from the process environment."""

    def __init__(self) -> None:
        self.host = os.getenv("INTERVIEWER_SMTP_HOST", "").strip()
        self.port = int(os.getenv("INTERVIEWER_SMTP_PORT", "587"))
        self.username = os.getenv("INTERVIEWER_SMTP_USERNAME", "").strip()
        self.password = os.getenv("INTERVIEWER_SMTP_PASSWORD", "")
        self.from_email = os.getenv("INTERVIEWER_SMTP_FROM_EMAIL", "").strip() or self.username
        self.use_ssl = _environment_bool("INTERVIEWER_SMTP_USE_SSL", False)
        self.starttls = _environment_bool("INTERVIEWER_SMTP_STARTTLS", not self.use_ssl)
        self.timeout_seconds = max(1, int(os.getenv("INTERVIEWER_SMTP_TIMEOUT_SECONDS", "15")))

    @property
    def configured(self) -> bool:
        return bool(self.host and self.from_email and self.username and self.password)

    def send(self, message: OutboundEmail) -> None:
        if not self.configured:
            raise RuntimeError("SMTP email delivery is not configured.")
        email = EmailMessage()
        email["From"] = self.from_email
        email["To"] = message.recipient
        email["Subject"] = message.subject
        email["Message-ID"] = message.message_id
        email.set_content(message.text_body)
        client_type = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
        with client_type(self.host, self.port, timeout=self.timeout_seconds) as client:
            if self.starttls:
                client.starttls()
            client.login(self.username, self.password)
            client.send_message(email)


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
