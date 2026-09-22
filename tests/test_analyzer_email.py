from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from anthropic.types import TextBlock

from agentsenate.analyzer import (
    ClaudeAnalyzer,
    CostCapExceeded,
    DeepSeekAnalyzer,
    _strip_code_fence,
    compact_items,
)
from agentsenate.emailer import ResendResearchMailer, render_digest, select_topics
from agentsenate.models import (
    InitiativeAnalysis,
    ResearchCitation,
    ResearchResult,
    SourceCategory,
    SourceItem,
)
from agentsenate.researcher import DeepSeekResearcher


def analysis(external_id: str, score: int, useful: bool = True) -> InitiativeAnalysis:
    return InitiativeAnalysis(
        external_id=external_id,
        is_useful=useful,
        topic_tags=["Transit/Parking"],
        impact_classification="Operational/Actionable",
        target_stakeholder="Transit Director",
        executive_summary="A concise summary.",
        actionability_score=score,
        evidence="A direct quote.",
        ranking_rationale="Concrete and feasible.",
    )


def test_actionability_score_is_clamped() -> None:
    item = analysis("high", 8)
    assert item.actionability_score == 5
    selected = select_topics(
        [analysis("low", 2), analysis("high", 5), analysis("noise", 5, useful=False)], 3, 8
    )
    assert [item.external_id for item in selected] == ["high"]


def test_digest_escapes_source_content(source_item: SourceItem) -> None:
    unsafe = source_item.model_copy(update={"title": "<script>alert(1)</script>"})
    _, html, text = render_digest(
        [analysis(source_item.external_id, 5)], [unsafe], datetime(2026, 9, 15, tzinfo=UTC)
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Sources:" in html
    assert str(source_item.source_url) in text


def test_digest_renders_grouped_sources(source_item: SourceItem) -> None:
    second = source_item.model_copy(
        update={
            "external_id": "t3_second",
            "title": "Another Hillside dining thread",
            "source_url": "https://www.reddit.com/r/UGA/comments/second",
        }
    )
    grouped = analysis(source_item.external_id, 5).model_copy(
        update={
            "source_ids": [source_item.external_id, second.external_id],
            "sentiment": "mixed",
            "recommended_action": "Ask Dining Services for service-time data.",
            "executive_summary": "Students are repeatedly discussing Hillside dining quality.",
        }
    )
    _, html, text = render_digest(
        [grouped], [source_item, second], datetime(2026, 9, 15, tzinfo=UTC)
    )
    assert "Another Hillside dining thread" in html
    assert "Sentiment: mixed" in text
    assert "Ask Dining Services" in text


def test_json_code_fence_is_removed() -> None:
    assert _strip_code_fence('```json\n{"initiatives": []}\n```') == '{"initiatives": []}'


def test_compact_items_shortens_forum_text(source_item: SourceItem) -> None:
    long_item = source_item.model_copy(update={"raw_text": "parking " * 200})
    corpus = compact_items([long_item])
    assert len(corpus) == 1
    assert corpus[0]["external_id"] == source_item.external_id
    assert len(corpus[0]["text"] or "") <= 280


def test_compact_items_respects_char_budget(source_item: SourceItem) -> None:
    items = [
        source_item.model_copy(update={"external_id": f"t3_{index}", "title": f"Post {index}"})
        for index in range(8)
    ]
    corpus = compact_items(items, max_chars=400)
    assert 0 < len(corpus) < len(items)


def test_analyzer_makes_one_request_and_drops_unknown_ids(
    source_item: SourceItem, monkeypatch: object
) -> None:
    calls: list[object] = []

    class FakeMessages:
        def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return type(
                "Message",
                (),
                {
                    "content": [
                        TextBlock(
                            type="text",
                            text=json.dumps(
                                {
                                    "initiatives": [
                                        {
                                            "external_id": source_item.external_id,
                                            "is_useful": True,
                                            "topic_tags": ["Transit/Parking"],
                                            "impact_classification": "Operational/Actionable",
                                            "target_stakeholder": "Transit Director",
                                            "executive_summary": "Extend late-night buses.",
                                            "actionability_score": 5,
                                            "trend_alert_flag": False,
                                            "evidence": "Could buses run until 2 AM?",
                                            "ranking_rationale": "Clear service request.",
                                        },
                                        {
                                            "external_id": "invented",
                                            "is_useful": True,
                                            "topic_tags": [],
                                            "impact_classification": "Symbolic",
                                            "target_stakeholder": "N/A",
                                            "executive_summary": "Invented.",
                                            "actionability_score": 5,
                                            "trend_alert_flag": False,
                                            "evidence": "none",
                                            "ranking_rationale": "none",
                                        },
                                    ]
                                }
                            ),
                        )
                    ]
                },
            )()

    analyzer = ClaudeAnalyzer("key", "claude-test", max_topics=8)
    monkeypatch.setattr(analyzer.client, "messages", FakeMessages())  # type: ignore[attr-defined]
    board = source_item.model_copy(
        update={
            "external_id": "usg_1",
            "source": "usg_board",
            "source_category": SourceCategory.BOARD_OF_REGENTS,
            "title": "Fee policy",
            "raw_text": "Student fees",
        }
    )
    results = analyzer.analyze([source_item, board])
    assert len(calls) == 1
    assert [item.external_id for item in results] == [source_item.external_id]
    try:
        analyzer.analyze([source_item])
    except CostCapExceeded:
        pass
    else:
        raise AssertionError("second Claude call should be blocked")
    assert len(calls) == 1


def test_compact_items_puts_board_docs_first(source_item: SourceItem) -> None:
    board = source_item.model_copy(
        update={
            "external_id": "usg_1",
            "source": "usg_board",
            "source_category": SourceCategory.BOARD_OF_REGENTS,
            "title": "Fee policy",
        }
    )
    corpus = compact_items([source_item, board])
    assert [row["external_id"] for row in corpus] == ["usg_1", source_item.external_id]


def test_cost_cap_blocks_api_call(source_item: SourceItem) -> None:
    analyzer = ClaudeAnalyzer("key", "claude-test", max_cost_usd=0.0001)
    calls = 0

    def boom(**kwargs: object) -> object:
        del kwargs
        nonlocal calls
        calls += 1
        raise AssertionError("Claude should not be called over the cost cap")

    analyzer.client.messages.create = boom  # type: ignore[method-assign]
    try:
        analyzer.analyze([source_item])
    except CostCapExceeded:
        pass
    else:
        raise AssertionError("expected CostCapExceeded")
    assert calls == 0


def test_compact_items_includes_other_schools_before_repeats(source_item: SourceItem) -> None:
    other_school = source_item.model_copy(
        update={"external_id": "other_school", "university_name": "Other University"}
    )
    repeat = source_item.model_copy(update={"external_id": "same_school"})
    corpus = compact_items([source_item, repeat, other_school])
    assert [row["external_id"] for row in corpus] == [
        source_item.external_id,
        "other_school",
        "same_school",
    ]


def test_deepseek_validates_json_and_filters_unknown_ids(source_item: SourceItem) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        content = json.dumps(
            {
                "initiatives": [
                    analysis(source_item.external_id, 5)
                    .model_copy(update={"source_ids": [source_item.external_id, "invented"]})
                    .model_dump(),
                    analysis("invented", 5).model_dump(),
                ]
            }
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    analyzer = DeepSeekAnalyzer(
        "test-key", client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    results = analyzer.analyze([source_item])
    assert [item.external_id for item in results] == [source_item.external_id]
    assert results[0].source_ids == [source_item.external_id]


def test_deepseek_researcher_uses_monitored_context() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "Recent monitored-source context" in body["messages"][1]["content"]
        content = json.dumps(
            {
                "subject": "Dining follow-up",
                "identified_initiative": "Hillside dining feedback",
                "assessment": "The monitored context shows a student dining signal.",
                "verification_status": "partially verified",
                "comparable_policies": [],
                "stakeholders": ["Dining Services"],
                "recommended_actions": ["Ask Dining Services for rush-hour data."],
                "unknowns": ["Whether the issue is campus-wide."],
                "citations": [
                    {
                        "title": "Hillside thread",
                        "url": "https://www.reddit.com/r/UGA/comments/hillside",
                        "supports": "Student dining sentiment",
                    }
                ],
            }
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = DeepSeekResearcher(
        "test-key", client=httpx.Client(transport=httpx.MockTransport(respond))
    ).research(
        "Can you look into this?",
        [],
        [
            {
                "title": "Hillside thread",
                "source_url": "https://www.reddit.com/r/UGA/comments/hillside",
            }
        ],
    )
    assert result.identified_initiative == "Hillside dining feedback"


def test_empty_deepseek_response_is_retryable(source_item: SourceItem) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
        )
    )
    with pytest.raises(RuntimeError, match="leaving items pending"):
        DeepSeekAnalyzer("test-key", client=client).analyze([source_item])


def test_research_reply_sets_thread_headers(monkeypatch: object) -> None:
    captured: dict[str, object] = {}

    def fake_send(params: dict[str, object], options: dict[str, object]) -> dict[str, str]:
        captured.update(params)
        captured["options"] = options
        return {"id": "email_123"}

    monkeypatch.setattr("resend.Emails.send", fake_send)  # type: ignore[attr-defined]
    result = ResearchResult(
        subject="Transit research",
        identified_initiative="Longer bus hours",
        assessment="Partially corroborated.",
        verification_status="partially verified",
        citations=[
            ResearchCitation(
                title="Source",
                url="https://example.edu",
                supports="Operating hours",
            )
        ],
    )
    email_id = ResendResearchMailer("key", "sender@example.edu").send_reply(
        result,
        "student@example.edu",
        "Daily digest",
        "<reply@example.edu>",
        "<digest@example.edu>",
        "stable-key",
    )
    assert email_id == "email_123"
    assert captured["headers"] == {
        "In-Reply-To": "<reply@example.edu>",
        "References": "<digest@example.edu> <reply@example.edu>",
    }
