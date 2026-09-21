from __future__ import annotations

import json
import logging

from agentsenate.config import Secrets, load_file_config
from agentsenate.gmail import GmailInboxPoller, GmailSender, gmail_reply_alias
from agentsenate.inbound import InboundGateway, InboundResearchService
from agentsenate.researcher import ClaudeResearcher
from agentsenate.storage import TursoStorage


def main() -> None:
    secrets = Secrets()
    config = load_file_config(secrets.agentsenate_config)
    logging.basicConfig(
        level=getattr(logging, secrets.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not secrets.gmail_address or not secrets.gmail_app_password:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD are required")
    storage = TursoStorage(secrets.turso_database_url, secrets.turso_auth_token)
    researcher = ClaudeResearcher(secrets.anthropic_api_key, secrets.anthropic_research_model)
    mailer = GmailSender(secrets.gmail_address, secrets.gmail_app_password)
    authorized = config.email.authorized_reply_senders or config.email.recipients

    def service_factory(gateway: InboundGateway) -> InboundResearchService:
        return InboundResearchService(
            gateway=gateway,
            storage=storage,
            researcher=researcher,
            mailer=mailer,
            authorized_senders=authorized,
            maximum_attachment_bytes=config.email.maximum_attachment_bytes,
            maximum_total_attachment_bytes=config.email.maximum_total_attachment_bytes,
        )

    result = GmailInboxPoller(
        address=secrets.gmail_address,
        app_password=secrets.gmail_app_password,
        reply_address=config.email.reply_to or gmail_reply_alias(secrets.gmail_address),
        service_factory=service_factory,
    ).poll()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
