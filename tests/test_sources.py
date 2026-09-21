from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pypdf import PdfWriter

from agentsenate.config import NewsletterConfig, RedditConfig, USGConfig, load_file_config
from agentsenate.models import NewsletterTarget, School, SourceCategory
from agentsenate.sources import NewsletterSource, RedditSource, USGBoardSource
from agentsenate.sources.usg import parse_meeting_date


class Router:
    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        del kwargs
        self.urls.append(url)
        if url in self.routes:
            return self.routes[url]
        for key, response in self.routes.items():
            if url.startswith(key):
                return response
        request = httpx.Request("GET", url)
        return httpx.Response(404, text="missing", request=request)


def _json_response(url: str, payload: dict[str, object], status: int = 200) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(status, json=payload, request=request)


def _text_response(
    url: str, text: str, status: int = 200, content_type: str = "text/html"
) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(
        status, text=text, headers={"content-type": content_type}, request=request
    )


def _pdf_bytes() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


def test_reddit_json_keeps_only_recent_posts() -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    payload = json.loads(Path("tests/fixtures/reddit_new.json").read_text(encoding="utf-8"))
    uga_new = "https://www.reddit.com/r/UGA/new.json?limit=100"
    uga_top = "https://www.reddit.com/r/UGA/top.json?limit=100&t=month"
    ufl_new = "https://www.reddit.com/r/ufl/new.json?limit=100"
    ufl_top = "https://www.reddit.com/r/ufl/top.json?limit=100&t=month"
    empty = {"kind": "Listing", "data": {"after": None, "children": []}}
    client = Router(
        {
            uga_new: _json_response(uga_new, payload),
            uga_top: _json_response(uga_top, empty),
            ufl_new: _json_response(ufl_new, empty),
            ufl_top: _json_response(ufl_top, empty),
        }
    )
    source = RedditSource(
        RedditConfig(min_request_interval_seconds=0, feeds=["new", "top_month"], max_pages=1),
        [
            School(name="University of Georgia", subreddits=["UGA"]),
            School(name="University of Florida", subreddits=["ufl"]),
        ],
        "agentsenate-test",
        client=client,  # type: ignore[arg-type]
        sleeper=lambda _: None,
    )
    items = source.fetch(now, timedelta(days=90))
    assert {item.external_id for item in items} == {"t3_abc123", "t3_old999"}
    assert client.urls == [uga_new, uga_top, ufl_new, ufl_top]
    recent = source.fetch(now, timedelta(hours=26))
    assert [item.external_id for item in recent] == ["t3_abc123"]
    assert items[0].title == "Extend late-night buses"
    assert items[0].author == "dawgfan"
    assert "buses run until 2 AM" in items[0].raw_text
    assert str(items[0].source_url).endswith("/extend_late_night_buses/")


def test_reddit_falls_back_to_old_reddit_after_403() -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    payload = json.loads(Path("tests/fixtures/reddit_new.json").read_text(encoding="utf-8"))
    blocked = "https://www.reddit.com/r/UGA/new.json?limit=100"
    fallback = "https://old.reddit.com/r/UGA/new.json?limit=100"
    client = Router(
        {
            blocked: _text_response(blocked, "Blocked", status=403),
            fallback: _json_response(fallback, payload),
        }
    )
    source = RedditSource(
        RedditConfig(min_request_interval_seconds=0, feeds=["new"], max_pages=1),
        [School(name="University of Georgia", subreddits=["UGA"])],
        "agentsenate-test",
        client=client,  # type: ignore[arg-type]
        sleeper=lambda _: None,
    )
    items = source.fetch(now, timedelta(days=30))
    assert {item.external_id for item in items} == {"t3_abc123", "t3_old999"}
    assert blocked in client.urls
    assert fallback in client.urls


def test_reddit_rss_fallback_when_json_is_not_json() -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    rss = Path("tests/fixtures/reddit_new.rss").read_text(encoding="utf-8")
    json_url = "https://www.reddit.com/r/UGA/new.json?limit=100"
    rss_url = "https://www.reddit.com/r/UGA/new/.rss"
    client = Router(
        {
            json_url: _text_response(json_url, "<html>nope</html>"),
            "https://old.reddit.com/r/UGA/new.json?limit=100": _text_response(
                "https://old.reddit.com/r/UGA/new.json?limit=100", "<html>nope</html>"
            ),
            rss_url: _text_response(rss_url, rss, content_type="application/rss+xml"),
        }
    )
    source = RedditSource(
        RedditConfig(min_request_interval_seconds=0, feeds=["new"], max_pages=1),
        [School(name="University of Georgia", subreddits=["UGA"])],
        "agentsenate-test",
        client=client,  # type: ignore[arg-type]
        sleeper=lambda _: None,
    )
    items = source.fetch(now, timedelta(days=90))
    assert {item.external_id for item in items} == {"t3_abc123", "t3_old999"}
    assert rss_url in client.urls


def test_usg_discovers_agenda_and_minutes_from_archive() -> None:
    fixture = Path("tests/fixtures/usg_archive.html").read_text(encoding="utf-8")
    pdf = _pdf_bytes()
    routes = {
        "https://example.test/2026/": _text_response("https://example.test/2026/", fixture),
        "https://example.test/2025/": _text_response("https://example.test/2025/", fixture),
        "https://example.test/documents/agenda.pdf": httpx.Response(
            200,
            content=pdf,
            headers={"content-type": "application/pdf"},
            request=httpx.Request("GET", "https://example.test/documents/agenda.pdf"),
        ),
        "https://example.test/documents/minutes.pdf": httpx.Response(
            200,
            content=pdf,
            headers={"content-type": "application/pdf"},
            request=httpx.Request("GET", "https://example.test/documents/minutes.pdf"),
        ),
    }
    client = Router(routes)
    source = USGBoardSource(
        USGConfig(archive_url="https://example.test/{year}/", min_request_interval_seconds=0),
        client=client,  # type: ignore[arg-type]
    )
    items = source.fetch(datetime(2026, 9, 15, tzinfo=UTC), timedelta(days=30))
    assert len(items) == 2
    assert {item.metadata["document_type"] for item in items} == {"agenda", "minutes"}
    assert all(item.title.startswith("USG Board of Regents") for item in items)


def test_usg_skips_old_meetings_and_classifies_notices() -> None:
    fixture = Path("tests/fixtures/usg_archive_mixed.html").read_text(encoding="utf-8")
    pdf = _pdf_bytes()
    pdf_urls = [
        "https://example.test/documents/old-agenda.pdf",
        "https://example.test/documents/old-minutes.pdf",
        "https://example.test/documents/agenda.pdf",
        "https://example.test/documents/minutes.pdf",
        "https://example.test/documents/BOR-Public-Notice-September-16-2026.pdf",
    ]
    routes: dict[str, httpx.Response] = {
        "https://example.test/2026/": _text_response("https://example.test/2026/", fixture),
        "https://example.test/2025/": _text_response("https://example.test/2025/", "<html></html>"),
    }
    for url in pdf_urls:
        routes[url] = httpx.Response(
            200,
            content=pdf,
            headers={"content-type": "application/pdf"},
            request=httpx.Request("GET", url),
        )
    client = Router(routes)
    source = USGBoardSource(
        USGConfig(archive_url="https://example.test/{year}/", min_request_interval_seconds=0),
        client=client,  # type: ignore[arg-type]
    )
    items = source.fetch(datetime(2026, 9, 21, tzinfo=UTC), timedelta(days=30))
    assert {item.metadata["document_type"] for item in items} == {"agenda", "minutes", "notice"}
    assert "https://example.test/documents/old-agenda.pdf" not in client.urls
    assert parse_meeting_date("April 14-15, 2026") == datetime(2026, 4, 14, tzinfo=UTC)


def test_newsletter_rss_filters_by_lookback() -> None:
    rss = Path("tests/fixtures/newsletter.rss").read_text(encoding="utf-8")
    listing = "https://news.example.edu/feed/"
    client = Router({listing: _text_response(listing, rss, content_type="application/rss+xml")})
    source = NewsletterSource(
        NewsletterConfig(min_request_interval_seconds=0, fetch_article_body=False),
        [
            School(
                name="Example University",
                newsletters=[NewsletterTarget(name="Campus News", url=listing)],
            )
        ],
        client=client,  # type: ignore[arg-type]
        sleeper=lambda _: None,
    )
    items = source.fetch(datetime(2026, 9, 15, 12, tzinfo=UTC), timedelta(days=30))
    assert len(items) == 1
    assert items[0].title == "Dining halls add late hours"
    assert items[0].source_category == SourceCategory.NEWSLETTER
    assert "midnight" in items[0].raw_text


def test_newsletter_html_listing_fetches_article_body() -> None:
    listing = "https://news.example.edu/"
    article = "https://news.example.edu/2026/09/bus-hours/"
    client = Router(
        {
            listing: _text_response(
                listing, Path("tests/fixtures/newsletter_listing.html").read_text(encoding="utf-8")
            ),
            article: _text_response(
                article, Path("tests/fixtures/newsletter_article.html").read_text(encoding="utf-8")
            ),
        }
    )
    source = NewsletterSource(
        NewsletterConfig(min_request_interval_seconds=0, fetch_article_body=True),
        [
            School(
                name="Example University",
                newsletters=[NewsletterTarget(name="Campus News", url=listing)],
            )
        ],
        client=client,  # type: ignore[arg-type]
        sleeper=lambda _: None,
    )
    items = source.fetch(datetime(2026, 9, 15, 12, tzinfo=UTC), timedelta(days=30))
    assert len(items) == 1
    assert items[0].title == "Campus bus hours expand"
    assert "Night routes" in items[0].raw_text
    assert items[0].university_name == "Example University"


def test_schools_yaml_includes_a_newsletter_for_every_school() -> None:
    config = load_file_config(Path("config/schools.yaml"))
    assert config.sources.newsletters.enabled is True
    missing = [school.name for school in config.schools if not school.newsletters]
    assert missing == []
