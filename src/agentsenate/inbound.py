from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from email.utils import parseaddr
from typing import Any, Protocol, cast

import httpx
import resend
from bs4 import BeautifulSoup

from agentsenate.models import InboundImage, ResearchResult

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


class InboundRejected(ValueError):
    pass


def verify_resend_webhook(
    payload: str, webhook_id: str, timestamp: str, signature: str, secret: str
) -> dict[str, Any]:
    resend.Webhooks.verify(
        {
            "payload": payload,
            "headers": {
                "id": webhook_id,
                "timestamp": timestamp,
                "signature": signature,
            },
            "webhook_secret": secret,
        }
    )
    return cast(dict[str, Any], json.loads(payload))


class Researcher(Protocol):
    def research(
        self, request_text: str, images: list[InboundImage], context: list[dict[str, Any]]
    ) -> ResearchResult: ...


class ResearchMailer(Protocol):
    def send_reply(
        self,
        result: ResearchResult,
        recipient: str,
        original_subject: str,
        message_id: str,
        references: str,
        idempotency_key: str,
    ) -> str: ...


class InboundStorage(Protocol):
    def claim_inbound(
        self,
        email_id: str,
        sender: str,
        subject: str,
        message_id: str,
        received_at: datetime,
        request_text: str,
        attachments: list[dict[str, Any]],
    ) -> bool: ...

    def complete_inbound(self, email_id: str, result: dict[str, Any], delivery_id: str) -> None: ...

    def fail_inbound(self, email_id: str, error: str) -> None: ...

    def recent_context(self, limit: int = 20) -> list[dict[str, Any]]: ...


class InboundGateway(Protocol):
    def get_email(self, email_id: str) -> dict[str, Any]: ...

    def list_attachments(self, email_id: str) -> list[dict[str, Any]]: ...

    def download(self, url: str) -> bytes: ...


class ResendInboundGateway:
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        resend.api_key = api_key
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)

    def get_email(self, email_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], resend.Emails.Receiving.get(email_id=email_id))

    def list_attachments(self, email_id: str) -> list[dict[str, Any]]:
        response = cast(
            dict[str, Any],
            resend.Emails.Receiving.Attachments.list(email_id=email_id),
        )
        return cast(list[dict[str, Any]], response.get("data", []))

    def download(self, url: str) -> bytes:
        response = self.client.get(url)
        response.raise_for_status()
        return response.content


def strip_quoted_text(text: str, maximum_chars: int = 20_000) -> str:
    lines = []
    for line in text.replace("\r\n", "\n").splitlines():
        lowered = line.strip().lower()
        if line.lstrip().startswith(">"):
            continue
        if re.match(r"^on .+wrote:$", lowered) or lowered.startswith("from:"):
            break
        lines.append(line)
    return "\n".join(lines).strip()[:maximum_chars]


class InboundResearchService:
    def __init__(
        self,
        gateway: InboundGateway,
        storage: InboundStorage,
        researcher: Researcher,
        mailer: ResearchMailer,
        authorized_senders: list[str],
        maximum_attachment_bytes: int,
        maximum_total_attachment_bytes: int,
    ) -> None:
        self.gateway = gateway
        self.storage = storage
        self.researcher = researcher
        self.mailer = mailer
        self.authorized_senders = {address.lower() for address in authorized_senders}
        self.maximum_attachment_bytes = maximum_attachment_bytes
        self.maximum_total_attachment_bytes = maximum_total_attachment_bytes

    def process(self, event: dict[str, Any]) -> dict[str, Any]:
        if event.get("type") != "email.received":
            return {"status": "ignored", "reason": "unsupported event"}
        data = cast(dict[str, Any], event["data"])
        email_id = str(data["email_id"])
        email = self.gateway.get_email(email_id)
        sender = parseaddr(str(email.get("from") or data["from"]))[1].lower()
        if sender not in self.authorized_senders:
            return {"status": "ignored", "reason": "unauthorized sender"}

        text = str(email.get("text") or "")
        if not text and email.get("html"):
            text = BeautifulSoup(str(email["html"]), "html.parser").get_text("\n", strip=True)
        request_text = strip_quoted_text(text)
        attachments = self.gateway.list_attachments(email_id)
        if len(attachments) > 5:
            raise InboundRejected("A maximum of five screenshots is allowed")

        total_size = 0
        attachment_records: list[dict[str, Any]] = []
        for attachment in attachments:
            media_type = str(attachment.get("content_type") or "").lower()
            size = int(attachment.get("size") or 0)
            if media_type not in ALLOWED_IMAGE_TYPES:
                raise InboundRejected(f"Unsupported attachment type: {media_type or 'unknown'}")
            if size > self.maximum_attachment_bytes:
                raise InboundRejected("An attachment exceeds the configured size limit")
            total_size += size
            attachment_records.append(
                {
                    "id": str(attachment.get("id") or ""),
                    "filename": str(attachment.get("filename") or "screenshot"),
                    "content_type": media_type,
                    "size": size,
                }
            )
        if total_size > self.maximum_total_attachment_bytes:
            raise InboundRejected("Attachments exceed the configured total size limit")
        if not request_text and not attachment_records:
            raise InboundRejected("Reply contained no text or supported screenshots")

        subject = str(data.get("subject") or "Initiative research")
        message_id = str(data["message_id"])
        received_at = datetime.fromisoformat(str(data["created_at"]).replace("Z", "+00:00"))
        claimed = self.storage.claim_inbound(
            email_id,
            sender,
            subject,
            message_id,
            received_at,
            request_text,
            attachment_records,
        )
        if not claimed:
            return {"status": "duplicate", "email_id": email_id}

        try:
            images = self._download_images(attachments)
            result = self.researcher.research(request_text, images, self.storage.recent_context())
            headers = cast(dict[str, str], email.get("headers") or {})
            delivery_id = self.mailer.send_reply(
                result=result,
                recipient=sender,
                original_subject=subject,
                message_id=message_id,
                references=headers.get("references", ""),
                idempotency_key=hashlib.sha256(email_id.encode()).hexdigest(),
            )
            self.storage.complete_inbound(email_id, result.model_dump(mode="json"), delivery_id)
            return {"status": "completed", "email_id": email_id, "delivery_id": delivery_id}
        except Exception as exc:
            self.storage.fail_inbound(email_id, str(exc))
            raise

    def _download_images(self, attachments: list[dict[str, Any]]) -> list[InboundImage]:
        images = []
        for attachment in attachments:
            content = self.gateway.download(str(attachment["download_url"]))
            if len(content) > self.maximum_attachment_bytes:
                raise ValueError("Downloaded attachment exceeds the configured size limit")
            images.append(
                InboundImage(
                    filename=str(attachment.get("filename") or "screenshot"),
                    media_type=str(attachment["content_type"]).lower(),
                    content=content,
                )
            )
        if sum(len(image.content) for image in images) > self.maximum_total_attachment_bytes:
            raise ValueError("Downloaded attachments exceed the configured total size limit")
        return images
