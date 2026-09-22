from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from agentsenate.models import InitiativeAnalysis, SourceItem
from agentsenate.storage import SupabaseRestStorage, TursoStorage


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


def test_supabase_reads_all_pages_even_when_server_caps_page_size(source_item: SourceItem) -> None:
    rows = [
        source_item.model_copy(update={"external_id": f"item_{i}"}).model_dump(mode="json")
        for i in range(5)
    ]
    offsets: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(200, json=rows[offset : offset + 2])

    storage = SupabaseRestStorage(
        "https://example.supabase.co",
        "test-key",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    assert len(storage.pending_items()) == 5
    assert offsets == [0, 2, 4, 5]


def test_supabase_batches_large_id_updates() -> None:
    filters: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        filters.append(request.url.params["external_id"])
        assert len(str(request.url)) < 8000
        return httpx.Response(204)

    storage = SupabaseRestStorage(
        "https://example.supabase.co",
        "test-key",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    storage.mark_baselined([f"{i:064x}" for i in range(101)], datetime.now(UTC))
    assert len(filters) == 3
    assert f"{100:064x}" in filters[-1]
