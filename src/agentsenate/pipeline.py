from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from agentsenate.emailer import render_digest, select_topics
from agentsenate.models import InitiativeAnalysis, SourceItem
from agentsenate.sources import SourceAdapter
from agentsenate.storage import Storage

logger = logging.getLogger(__name__)


class Analyzer(Protocol):
    def analyze(self, items: list[SourceItem]) -> list[InitiativeAnalysis]: ...


class Mailer(Protocol):
    def send(self, subject: str, html: str, text: str, idempotency_key: str) -> str: ...


class Pipeline:
    def __init__(
        self,
        sources: dict[str, SourceAdapter],
        storage: Storage,
        analyzer: Analyzer,
        mailer: Mailer | None,
        minimum_score: int,
        maximum_topics: int,
        lookback: timedelta | None = None,
        preview_writer: Callable[[str], None] | None = None,
        send_initial_digest: bool = False,
    ) -> None:
        self.sources = sources
        self.storage = storage
        self.analyzer = analyzer
        self.mailer = mailer
        self.minimum_score = minimum_score
        self.maximum_topics = maximum_topics
        self.lookback = lookback or timedelta(hours=26)
        self.preview_writer = preview_writer
        self.send_initial_digest = send_initial_digest

    def run(self, now: datetime | None = None) -> dict[str, object]:
        now = now or datetime.now(UTC)
        run_id = self.storage.start_run(now)
        source_errors: dict[str, str] = {}
        fetched: list[SourceItem] = []
        try:
            for name, source in self.sources.items():
                try:
                    source_items = source.fetch(now, self.lookback)
                except Exception as exc:
                    logger.exception("Source %s failed", name)
                    source_errors[name] = str(exc)
                else:
                    self.storage.store_items(source_items)
                    fetched.extend(source_items)
                    logger.info("Stored %s items from %s", len(source_items), name)
            pending = self.storage.pending_items()

            if not self.storage.has_successful_run() and not self.send_initial_digest:
                self.storage.mark_baselined([item.external_id for item in pending], now)
                details: dict[str, object] = {
                    "bootstrap": True,
                    "fetched": len(fetched),
                    "baselined": len(pending),
                    "source_errors": source_errors,
                    "sent": False,
                }
                status = "partial" if source_errors else "success"
                self.storage.finish_run(run_id, status, details)
                return details

            if not pending:
                details = {
                    "fetched": len(fetched),
                    "pending": 0,
                    "source_errors": source_errors,
                    "sent": False,
                }
                status = "partial" if source_errors else "success"
                self.storage.finish_run(run_id, status, details)
                return details

            analyses = self.analyzer.analyze(pending)
            selected = select_topics(analyses, self.minimum_score, self.maximum_topics)
            send_id: str | None = None
            if selected:
                subject, html, text = render_digest(selected, pending, now)
                key_material = ",".join(sorted(item.external_id for item in selected))
                idempotency_key = hashlib.sha256(key_material.encode()).hexdigest()
                if self.preview_writer is not None:
                    self.preview_writer(html)
                if self.mailer is not None:
                    send_id = self.mailer.send(subject, html, text, idempotency_key)
                    selected_ids = [item.external_id for item in selected]
                    self.storage.record_delivery(idempotency_key, selected_ids, send_id, now)
                    self.storage.mark_emailed(selected_ids, now)
            self.storage.mark_screened(analyses, now)
            leftover = [
                item.external_id
                for item in pending
                if item.external_id not in {analysis.external_id for analysis in analyses}
            ]
            self.storage.mark_baselined(leftover, now)
            details = {
                "fetched": len(fetched),
                "analyzed": len(analyses),
                "selected": len(selected),
                "source_errors": source_errors,
                "sent": send_id is not None,
                "email_id": send_id,
            }
            status = "partial" if source_errors else "success"
            self.storage.finish_run(run_id, status, details)
            return details
        except Exception as exc:
            self.storage.finish_run(
                run_id, "failed", {"error": str(exc), "source_errors": source_errors}
            )
            raise
