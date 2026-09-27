from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
from pydantic import HttpUrl

from agentsenate.config import InstagramConfig
from agentsenate.models import School, SourceCategory, SourceItem
from agentsenate.sources.common import (
    DEFAULT_USER_AGENT,
    RateLimitedClient,
    content_hash,
    is_sports_noise,
    parse_datetime,
)

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v22.0"


class InstagramSource:
    """Official campus Instagram posts via Graph API business discovery. No comment scrape."""

    def __init__(
        self,
        config: InstagramConfig,
        schools: list[School],
        access_token: str = "",
        business_account_id: str = "",
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.schools = schools
        self.access_token = access_token
        self.business_account_id = business_account_id
        http_client = client or httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        )
        self.http = RateLimitedClient(
            http_client,
            config.min_request_interval_seconds,
            sleeper or time.sleep,
        )

    def fetch(self, now: datetime, lookback: timedelta) -> list[SourceItem]:
        if not self.config.enabled:
            return []
        if not self.access_token or not self.business_account_id:
            logger.warning(
                "Instagram skipped: set INSTAGRAM_ACCESS_TOKEN and "
                "INSTAGRAM_BUSINESS_ACCOUNT_ID"
            )
            return []
        cutoff = now - lookback
        items: list[SourceItem] = []
        for school in self.schools:
            for raw in school.instagram:
                username = raw.strip().lstrip("@")
                if not username:
                    continue
                try:
                    items.extend(self._fetch_account(school, username, now, cutoff))
                except httpx.HTTPError as exc:
                    logger.warning("Instagram @%s failed: %s", username, exc)
        unique: dict[str, SourceItem] = {}
        for item in items:
            unique.setdefault(item.external_id, item)
        return list(unique.values())

    def _fetch_account(
        self,
        school: School,
        username: str,
        observed_at: datetime,
        cutoff: datetime,
    ) -> list[SourceItem]:
        fields = (
            f"business_discovery.username({username})"
            f"{{username,media.limit({self.config.max_posts_per_account})"
            f"{{id,caption,permalink,timestamp,media_type}}}}"
        )
        response = self.http.get(
            f"https://graph.facebook.com/{GRAPH_VERSION}/{self.business_account_id}",
            params={"fields": fields, "access_token": self.access_token},
        )
        payload = response.json()
        if payload.get("error"):
            logger.warning("Instagram @%s: %s", username, payload["error"])
            return []
        discovery = payload.get("business_discovery") or {}
        media = ((discovery.get("media") or {}).get("data") or [])
        items: list[SourceItem] = []
        for post in media:
            caption = str(post.get("caption") or "").strip()
            permalink = str(post.get("permalink") or "")
            if not permalink:
                continue
            published_at = parse_datetime(str(post.get("timestamp") or ""))
            if published_at is not None and published_at < cutoff:
                continue
            title = caption.split("\n", 1)[0][:180] or f"@{username} Instagram post"
            if self.config.drop_sports and is_sports_noise(title, caption):
                continue
            media_id = str(post.get("id") or permalink)
            items.append(
                SourceItem(
                    source="instagram",
                    source_category=SourceCategory.SOCIAL,
                    external_id=content_hash(media_id),
                    source_url=HttpUrl(permalink),
                    observed_at=observed_at,
                    published_at=published_at,
                    university_name=school.name,
                    title=title,
                    raw_text=caption[:8_000],
                    metadata={
                        "username": username,
                        "media_type": post.get("media_type"),
                    },
                    content_hash=content_hash(permalink, caption[:1_000]),
                )
            )
        return items
