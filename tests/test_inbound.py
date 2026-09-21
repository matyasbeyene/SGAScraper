from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import pytest

from agentsenate.inbound import (
    InboundRejected,
    InboundResearchService,
    strip_quoted_text,
    verify_resend_webhook,
)
from agentsenate.models import ResearchCitation, ResearchResult
from agentsenate.storage import TursoStorage


class FakeGateway:
    content_type = "image/png"

    def get_email(self, email_id: str) -> dict[str, Any]:
        assert email_id == "received_123"
        return {
            "from": "Student <student@example.edu>",
            "text": "Please research this.\n\nOn Tue, Person wrote:\n> old message",
            "headers": {"references": "<digest@example.org>"},
        }

    def list_attachments(self, email_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "attachment_123",
                "filename": "yikyak.png",
                "content_type": self.content_type,
                "size": 7,
                "download_url": "https://example.test/yikyak.png",
            }
        ]

    def download(self, url: str) -> bytes:
        assert url == "https://example.test/yikyak.png"
        return b"pngdata"


class FakeResearcher:
    def research(
        self, request_text: str, images: list[Any], context: list[dict[str, Any]]
    ) -> ResearchResult:
        assert request_text == "Please research this."
        assert images[0].media_type == "image/png"
        return ResearchResult(
            subject="Late-night transit",
            identified_initiative="Extend bus hours",
            assessment="The screenshot is an unverified student signal.",
            verification_status="partially verified",
            citations=[
                ResearchCitation(
                    title="Transit plan",
                    url="https://example.edu/transit",
                    supports="Current operating hours.",
                )
            ],
        )


class FakeMailer:
    calls = 0
    headers: tuple[str, str] | None = None

    def send_reply(
        self,
        result: ResearchResult,
        recipient: str,
        original_subject: str,
        message_id: str,
        references: str,
        idempotency_key: str,
    ) -> str:
        assert recipient == "student@example.edu"
        assert len(idempotency_key) == 64
        self.calls += 1
        self.headers = (message_id, references)
        return "sent_123"


def load_event() -> dict[str, Any]:
    return json.loads(Path("tests/fixtures/resend_email_received.json").read_text(encoding="utf-8"))


def make_service(gateway: FakeGateway | None = None) -> tuple[InboundResearchService, FakeMailer]:
    storage = TursoStorage(":memory:", "")
    storage.connection.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
    mailer = FakeMailer()
    service = InboundResearchService(
        gateway=gateway or FakeGateway(),  # type: ignore[arg-type]
        storage=storage,
        researcher=FakeResearcher(),
        mailer=mailer,
        authorized_senders=["student@example.edu"],
        maximum_attachment_bytes=100,
        maximum_total_attachment_bytes=200,
    )
    return service, mailer


def test_reply_is_researched_once_and_thread_metadata_is_preserved() -> None:
    service, mailer = make_service()
    first = service.process(load_event())
    second = service.process(load_event())
    assert first["status"] == "completed"
    assert second["status"] == "duplicate"
    assert mailer.calls == 1
    assert mailer.headers == ("<reply-123@example.edu>", "<digest@example.org>")


def test_unsupported_attachment_is_rejected() -> None:
    gateway = FakeGateway()
    gateway.content_type = "application/pdf"
    service, _ = make_service(gateway)
    with pytest.raises(InboundRejected):
        service.process(load_event())


def test_quoted_history_is_removed() -> None:
    assert strip_quoted_text("New request\n\nOn Tue, A wrote:\n> old") == "New request"


def test_webhook_signature_verification() -> None:
    payload = json.dumps({"type": "email.received", "data": {}})
    webhook_id = "msg_test"
    timestamp = str(int(time.time()))
    key = b"test-secret"
    secret = "whsec_" + base64.b64encode(key).decode()
    signed = f"{webhook_id}.{timestamp}.{payload}".encode()
    signature = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    assert (
        verify_resend_webhook(payload, webhook_id, timestamp, signature, secret)["type"]
        == "email.received"
    )
    with pytest.raises(ValueError):
        verify_resend_webhook(payload + " ", webhook_id, timestamp, signature, secret)
