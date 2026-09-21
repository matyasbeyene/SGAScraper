from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from agentsenate.config import Secrets, load_file_config
from agentsenate.emailer import ResendResearchMailer
from agentsenate.inbound import (
    InboundRejected,
    InboundResearchService,
    ResendInboundGateway,
    verify_resend_webhook,
)
from agentsenate.researcher import ClaudeResearcher
from agentsenate.storage import SupabaseRestStorage, TursoStorage


def build_service() -> InboundResearchService:
    secrets = Secrets()
    config = load_file_config(secrets.agentsenate_config)
    authorized = config.email.authorized_reply_senders or config.email.recipients
    if not authorized:
        raise RuntimeError("No authorized reply senders are configured")
    return InboundResearchService(
        gateway=ResendInboundGateway(secrets.resend_api_key),
        storage=(
            TursoStorage(secrets.turso_database_url, secrets.turso_auth_token)
            if secrets.storage_backend == "turso"
            else SupabaseRestStorage(secrets.supabase_url, secrets.supabase_service_role_key)
        ),
        researcher=ClaudeResearcher(secrets.anthropic_api_key, secrets.anthropic_research_model),
        mailer=ResendResearchMailer(secrets.resend_api_key, secrets.resend_from),
        authorized_senders=authorized,
        maximum_attachment_bytes=config.email.maximum_attachment_bytes,
        maximum_total_attachment_bytes=config.email.maximum_total_attachment_bytes,
    )


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("content-length", "0"))
            if length <= 0 or length > 1_000_000:
                self._respond(413, {"error": "Invalid webhook payload size"})
                return
            payload = self.rfile.read(length).decode("utf-8")
            secrets = Secrets()
            event = verify_resend_webhook(
                payload,
                self.headers.get("svix-id", ""),
                self.headers.get("svix-timestamp", ""),
                self.headers.get("svix-signature", ""),
                secrets.resend_webhook_secret,
            )
            try:
                result = build_service().process(event)
            except InboundRejected as exc:
                result = {"status": "rejected", "reason": str(exc)}
            self._respond(200, result)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400, {"error": "Invalid webhook"})
        except Exception:
            self._respond(500, {"error": "Processing failed"})

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
