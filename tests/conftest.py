from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentsenate.models import SourceCategory, SourceItem
from agentsenate.sources import content_hash


@pytest.fixture
def source_item() -> SourceItem:
    return SourceItem(
        source="reddit",
        source_category=SourceCategory.FORUM,
        external_id="t3_example",
        source_url="https://www.reddit.com/r/example/comments/1",
        observed_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
        published_at=datetime(2026, 9, 15, 11, tzinfo=UTC),
        university_name="Example University",
        title="Extend late-night buses",
        author="student",
        raw_text="Could buses run until 2 AM?",
        content_hash=content_hash("t3_example", "Could buses run until 2 AM?"),
    )
