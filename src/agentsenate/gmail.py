from __future__ import annotations

import hashlib
import imaplib
import logging
import smtplib
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from typing import Any, cast

from agentsenate.emailer import render_research
from agentsenate.inbound import InboundRejected
from agentsenate.models import ResearchResult

logger = logging.getLogger(__name__)


class GmailSender:
    def __init__(
        self,
        address: str,
        app_password: str,
        recipients: list[str] | None = None,
        reply_to: str = "",
    ) -> None:
        if not address or not app_password:
            raise RuntimeError("Gmail address and app password are required")
        self.address = address
        self.app_password = app_password.replace(" ", "")
        self.recipients = recipients or []
        self.reply_to = reply_to or gmail_reply_alias(address)

    def send(self, subject: str, html: str, text: str, idempotency_key: str) -> str:
        if not self.recipients:
            raise RuntimeError("At least one email recipient must be configured")
        message = self._message(subject, html, text, self.recipients, idempotency_key)
        message["Reply-To"] = self.reply_to
        self._deliver(message)
        return str(message["Message-ID"])

    def send_reply(
        self,
        result: ResearchResult,
        recipient: str,
        original_subject: str,
        message_id: str,
        references: str,
        idempotency_key: str,
    ) -> str:
        html, text = render_research(result)
        subject = (
            original_subject
            if original_subject.lower().startswith("re:")
            else f"Re: {original_subject}"
        )
        message = self._message(subject, html, text, [recipient], idempotency_key)
        message["In-Reply-To"] = message_id
        message["References"] = " ".join(part for part in (references, message_id) if part)
        self._deliver(message)
        return str(message["Message-ID"])

    def _message(
        self,
        subject: str,
        html: str,
        text: str,
        recipients: list[str],
        idempotency_key: str,
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self.address
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(idstring=idempotency_key[:32])
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        return message

    def _deliver(self, message: EmailMessage) -> None:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(self.address, self.app_password)
            smtp.send_message(message)


def gmail_reply_alias(address: str) -> str:
    local, domain = address.rsplit("@", 1)
    return f"{local}+agentsenate@{domain}"


class GmailMessageGateway:
    def __init__(self, message: EmailMessage, email_id: str) -> None:
        self.message = message
        self.email_id = email_id
        self.attachments: list[dict[str, Any]] = []
        self.contents: dict[str, bytes] = {}
        self._index_attachments()

    def get_email(self, email_id: str) -> dict[str, Any]:
        self._require_id(email_id)
        return {
            "from": str(self.message.get("From", "")),
            "text": self._body("plain"),
            "html": self._body("html"),
            "headers": {"references": str(self.message.get("References", ""))},
        }

    def list_attachments(self, email_id: str) -> list[dict[str, Any]]:
        self._require_id(email_id)
        return self.attachments

    def download(self, url: str) -> bytes:
        return self.contents[url]

    def _require_id(self, email_id: str) -> None:
        if email_id != self.email_id:
            raise KeyError("Unknown Gmail message")

    def _body(self, subtype: str) -> str:
        body = self.message.get_body(preferencelist=(subtype,))
        return body.get_content() if body is not None else ""

    def _index_attachments(self) -> None:
        for index, part in enumerate(self.message.iter_attachments()):
            payload = part.get_payload(decode=True)
            content = payload if isinstance(payload, bytes) else b""
            url = f"gmail-attachment://{self.email_id}/{index}"
            self.contents[url] = content
            self.attachments.append(
                {
                    "id": str(index),
                    "filename": part.get_filename() or f"screenshot-{index}",
                    "content_type": part.get_content_type(),
                    "size": len(content),
                    "download_url": url,
                }
            )


class GmailInboxPoller:
    def __init__(
        self,
        address: str,
        app_password: str,
        reply_address: str,
        service_factory: Any,
    ) -> None:
        self.address = address
        self.app_password = app_password.replace(" ", "")
        self.reply_address = reply_address.lower()
        self.service_factory = service_factory

    def poll(self) -> dict[str, int]:
        stats = {"found": 0, "processed": 0, "failed": 0}
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as mailbox:
            mailbox.login(self.address, self.app_password)
            mailbox.select("INBOX")
            status, data = cast(Any, mailbox).uid("search", None, "UNSEEN")
            if status != "OK":
                raise RuntimeError("Gmail inbox search failed")
            for uid in data[0].split():
                status, response = mailbox.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not response or not isinstance(response[0], tuple):
                    stats["failed"] += 1
                    continue
                message = message_from_bytes(response[0][1], policy=default)
                recipients = f"{message.get('To', '')},{message.get('Cc', '')}".lower()
                if self.reply_address not in recipients:
                    continue
                stats["found"] += 1
                email_id = (
                    "gmail_"
                    + hashlib.sha256(
                        str(message.get("Message-ID") or uid.decode()).encode()
                    ).hexdigest()
                )
                gateway = GmailMessageGateway(message, email_id)
                date_header = str(message.get("Date", ""))
                received_at = (
                    parsedate_to_datetime(date_header).isoformat()
                    if date_header
                    else datetime.now(UTC).isoformat()
                )
                event = {
                    "type": "email.received",
                    "data": {
                        "email_id": email_id,
                        "created_at": received_at,
                        "from": str(message.get("From", "")),
                        "message_id": str(message.get("Message-ID", "")),
                        "subject": str(message.get("Subject", "Initiative research")),
                    },
                }
                try:
                    result = self.service_factory(gateway).process(event)
                    if result["status"] in {
                        "completed",
                        "duplicate",
                        "ignored",
                        "rejected",
                    }:
                        mailbox.uid("store", uid, "+FLAGS", "(\\Seen)")
                    stats["processed"] += 1
                except InboundRejected:
                    mailbox.uid("store", uid, "+FLAGS", "(\\Seen)")
                    stats["processed"] += 1
                except Exception:
                    logger.exception("Failed to process Gmail message %s", email_id)
                    stats["failed"] += 1
        return stats
