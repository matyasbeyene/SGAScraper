from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import HttpUrl

from agentsenate.config import HackathonConfig
from agentsenate.models import School, SourceCategory, SourceItem
from agentsenate.sources.common import (
    DEFAULT_USER_AGENT,
    RateLimitedClient,
    content_hash,
    is_campus_signal,
)

logger = logging.getLogger(__name__)

SOFTWARE_ID = re.compile(r"software_(\d+)")
SOFTWARE_HREF = re.compile(r"/software/[\w-]+", re.IGNORECASE)


class HackathonSource:
    """Public Devpost galleries. Keeps prize winners only if they look like SGA initiatives."""

    def __init__(
        self,
        config: HackathonConfig,
        schools: list[School],
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.schools = schools
        http_client = client or httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        self.http = RateLimitedClient(
            http_client,
            config.min_request_interval_seconds,
            sleeper or time.sleep,
        )

    def fetch(self, now: datetime, lookback: timedelta) -> list[SourceItem]:
        del lookback
        if not self.config.enabled:
            return []
        items: list[SourceItem] = []
        for school in self.schools:
            for target in school.hackathons:
                try:
                    items.extend(self._fetch_gallery(school, target.name, str(target.url), now))
                except httpx.HTTPError as exc:
                    logger.warning("Hackathon gallery %s failed: %s", target.url, exc)
        unique: dict[str, SourceItem] = {}
        for item in items:
            unique.setdefault(item.external_id, item)
        if not unique:
            logger.info("No SGA-related hackathon winners found")
        return list(unique.values())

    def _fetch_gallery(
        self, school: School, event_name: str, url: str, observed_at: datetime
    ) -> list[SourceItem]:
        soup = BeautifulSoup(self.http.get(url).text, "html.parser")
        items: list[SourceItem] = []
        for entry in soup.select(".software-entry"):
            if not isinstance(entry, Tag):
                continue
            text = entry.get_text(" ", strip=True)
            is_winner = bool(entry.select(".winner")) or bool(
                re.search(r"(?<!not a )\bwinner\b", text, re.I)
            )
            if self.config.require_winner and not is_winner:
                continue
            title = ""
            img = entry.find("img")
            if isinstance(img, Tag):
                title = str(img.get("alt") or "").strip()
            if not title:
                heading = entry.find(["h5", "h3", "h2"])
                title = heading.get_text(" ", strip=True) if heading else text[:80]
            if self.config.require_sga_relevance and not is_campus_signal(title, text):
                continue
            parent = entry.find_parent("a")
            raw_href = str(parent.get("href") or "") if isinstance(parent, Tag) else ""
            href = urljoin(url, raw_href)
            if not raw_href or not SOFTWARE_HREF.search(href):
                continue
            match = SOFTWARE_ID.search(str(entry.get("id") or ""))
            external_id = f"hackathon_{match.group(1)}" if match else content_hash(href)
            items.append(
                SourceItem(
                    source="hackathon",
                    source_category=SourceCategory.HACKATHON,
                    external_id=external_id,
                    source_url=HttpUrl(href),
                    observed_at=observed_at,
                    university_name=school.name,
                    title=title,
                    raw_text=text[:8_000],
                    metadata={"event": event_name, "winner": is_winner, "gallery_url": url},
                    content_hash=content_hash(href, title, text[:1_000]),
                )
            )
        return items
