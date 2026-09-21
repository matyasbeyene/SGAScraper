from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agentsenate.models import InitiativeAnalysis, SourceItem
from agentsenate.storage import TursoStorage


def test_turso_storage_lifecycle(source_item: SourceItem) -> None:
    storage = TursoStorage(":memory:", "")
    schema = Path("db/schema.sql").read_text(encoding="utf-8")
    storage.connection.executescript(schema)

    assert storage.has_successful_run() is False
    run_id = storage.start_run(datetime(2026, 9, 16, tzinfo=UTC))
    storage.finish_run(run_id, "success", {"fetched": 1})
    assert storage.has_successful_run() is True

    storage.store_items([source_item, source_item])
    pending = storage.pending_items()
    assert [item.external_id for item in pending] == [source_item.external_id]

    result = InitiativeAnalysis(
        external_id=source_item.external_id,
        is_useful=True,
        impact_classification="Operational/Actionable",
        target_stakeholder="Transit Director",
        executive_summary="Extend late-night buses.",
        actionability_score=5,
        evidence="Buses until 2 AM.",
        ranking_rationale="Concrete proposal.",
    )
    now = datetime(2026, 9, 16, 12, tzinfo=UTC)
    storage.mark_screened([result], now)
    storage.mark_emailed([source_item.external_id], now)
    storage.record_delivery("digest-key", [source_item.external_id], "email-id", now)

    assert storage.pending_items() == []
    delivery = storage.connection.execute(
        "SELECT provider_id FROM digest_deliveries WHERE idempotency_key = ?",
        ("digest-key",),
    ).fetchone()
    assert delivery == ("email-id",)
