from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentsenate.models import InitiativeAnalysis, SourceItem
from agentsenate.pipeline import Pipeline
from agentsenate.storage import MemoryStorage


class StaticSource:
    def __init__(self, item: SourceItem) -> None:
        self.item = item

    def fetch(self, now: datetime, lookback: timedelta) -> list[SourceItem]:
        del now, lookback
        return [self.item]


class StaticAnalyzer:
    def analyze(self, items: list[SourceItem]) -> list[InitiativeAnalysis]:
        if not items:
            return []
        item = items[0]
        return [
            InitiativeAnalysis(
                external_id=item.external_id,
                is_useful=True,
                topic_tags=["Transit/Parking"],
                impact_classification="Operational/Actionable",
                target_stakeholder="Parking Director",
                executive_summary="Students proposed extending late-night bus service.",
                actionability_score=5,
                evidence="Could buses run until 2 AM?",
                ranking_rationale="A concrete service change with a clear owner.",
            )
        ]


class RecordingMailer:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, subject: str, html: str, text: str, idempotency_key: str) -> str:
        assert subject and html and text and len(idempotency_key) == 64
        self.calls += 1
        return "email_123"


def test_pipeline_sends_selected_item_once(source_item: SourceItem) -> None:
    storage = MemoryStorage(previously_ran=True)
    mailer = RecordingMailer()
    pipeline = Pipeline(
        sources={"reddit": StaticSource(source_item)},
        storage=storage,
        analyzer=StaticAnalyzer(),
        mailer=mailer,
        minimum_score=3,
        maximum_topics=8,
    )
    first = pipeline.run(datetime(2026, 9, 15, 12, tzinfo=UTC))
    second = pipeline.run(datetime(2026, 9, 15, 13, tzinfo=UTC))
    assert first["sent"] is True
    assert second["pending"] == 0
    assert mailer.calls == 1


def test_first_persistent_run_creates_baseline(source_item: SourceItem) -> None:
    storage = MemoryStorage(previously_ran=False)
    mailer = RecordingMailer()
    result = Pipeline(
        sources={"reddit": StaticSource(source_item)},
        storage=storage,
        analyzer=StaticAnalyzer(),
        mailer=mailer,
        minimum_score=3,
        maximum_topics=8,
    ).run(datetime(2026, 9, 15, 12, tzinfo=UTC))
    assert result["bootstrap"] is True
    assert result["baselined"] == 1
    assert mailer.calls == 0


def test_unselected_items_are_not_left_pending(source_item: SourceItem) -> None:
    extra = source_item.model_copy(update={"external_id": "t3_other", "title": "Dining hours"})
    storage = MemoryStorage(previously_ran=True)
    result = Pipeline(
        sources={"reddit": StaticSource(source_item), "usg": StaticSource(extra)},
        storage=storage,
        analyzer=StaticAnalyzer(),
        mailer=RecordingMailer(),
        minimum_score=3,
        maximum_topics=8,
    ).run(datetime(2026, 9, 15, 12, tzinfo=UTC))
    assert result["sent"] is True
    assert storage.pending_items() == []


def test_initial_digest_opt_in_sends_once(source_item: SourceItem) -> None:
    storage = MemoryStorage(previously_ran=False)
    mailer = RecordingMailer()
    pipeline = Pipeline(
        sources={"reddit": StaticSource(source_item)},
        storage=storage,
        analyzer=StaticAnalyzer(),
        mailer=mailer,
        minimum_score=3,
        maximum_topics=8,
        send_initial_digest=True,
    )
    assert pipeline.run(datetime(2026, 9, 15, 12, tzinfo=UTC))["sent"] is True
    assert pipeline.run(datetime(2026, 9, 15, 13, tzinfo=UTC))["sent"] is False
    assert mailer.calls == 1
