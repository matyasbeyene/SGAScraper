from __future__ import annotations

from agentsenate.models import InboundImage
from agentsenate.researcher import build_research_content


def test_research_content_contains_context_and_base64_image() -> None:
    content = build_research_content(
        "Research this post",
        [InboundImage(filename="yak.png", media_type="image/png", content=b"image")],
        [{"title": "Related transit initiative"}],
    )
    assert "Research this post" in content[0]["text"]
    assert "Related transit initiative" in content[0]["text"]
    assert content[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "aW1hZ2U=",
    }
