from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl

from agentsenate.config import RedditConfig
from agentsenate.models import School, SourceCategory, SourceItem
from agentsenate.sources.common import (
    ATOM_NS,
    RateLimitedClient,
    content_hash,
    parse_datetime,
    xml_link,
    xml_text,
)

logger = logging.getLogger(__name__)

REDDIT_POST_ID = re.compile(r"/comments/([a-z0-9]+)/", re.IGNORECASE)
JSON_FEEDS = {
    "new": ("new.json", {}),
    "hot": ("hot.json", {}),
    "top_week": ("top.json", {"t": "week"}),
    "top_month": ("top.json", {"t": "month"}),
    "top_year": ("top.json", {"t": "year"}),
}
RSS_FEEDS = {
    "new": "/r/{subreddit}/new/.rss",
    "hot": "/r/{subreddit}/.rss",
    "top_week": "/r/{subreddit}/top/.rss?t=week",
    "top_month": "/r/{subreddit}/top/.rss?t=month",
    "top_year": "/r/{subreddit}/top/.rss?t=year",
}
HOSTS = ("https://www.reddit.com", "https://old.reddit.com")


class RedditSource:
    """Public subreddit listings via JSON, with RSS as a fallback. No OAuth required."""

    def __init__(
        self,
        config: RedditConfig,
        schools: list[School],
        user_agent: str,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.schools = schools
        self.http = RateLimitedClient(
            client
            or httpx.Client(
                timeout=8,
                follow_redirects=True,
                headers={"User-Agent": user_agent},
            ),
            config.min_request_interval_seconds,
            sleeper,
        )

    def fetch(self, now: datetime, lookback: timedelta) -> list[SourceItem]:
        if not self.config.enabled or not self.schools:
            return []
        if self.config.include_top_level_comments:
            logger.info("Reddit public listings monitor posts only; comments are skipped")
        cutoff = now - lookback
        items: list[SourceItem] = []
        seen_subreddits: set[str] = set()
        feed_names = self.config.feeds or ["new"]
        for school in self.schools:
            for subreddit in school.subreddits:
                key = subreddit.casefold()
                if key in seen_subreddits:
                    continue
                seen_subreddits.add(key)
                for feed_name in feed_names:
                    items.extend(self._fetch_feed(school, subreddit, feed_name, now, cutoff))
        unique: dict[str, SourceItem] = {}
        for item in items:
            unique.setdefault(item.external_id, item)
        return list(unique.values())

    def _fetch_feed(
        self,
        school: School,
        subreddit: str,
        feed_name: str,
        observed_at: datetime,
        cutoff: datetime,
    ) -> list[SourceItem]:
        if feed_name not in JSON_FEEDS and feed_name not in RSS_FEEDS:
            logger.warning("Unknown Reddit feed %s", feed_name)
            return []
        last_error: Exception | None = None
        for host in (() if self.config.rss_only else HOSTS):
            try:
                return self._paginate_json(host, school, subreddit, feed_name, observed_at, cutoff)
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
                last_error = exc
                logger.info("Reddit JSON %s/%s on %s failed: %s", subreddit, feed_name, host, exc)
        for host in (HOSTS[:1] if self.config.rss_only else HOSTS):
            try:
                return self._fetch_rss(host, school, subreddit, feed_name, observed_at, cutoff)
            except (httpx.HTTPError, ET.ParseError) as exc:
                last_error = exc
                logger.info("Reddit RSS %s/%s on %s failed: %s", subreddit, feed_name, host, exc)
        logger.warning("Reddit feed %s/%s failed: %s", subreddit, feed_name, last_error)
        return []

    def _paginate_json(
        self,
        host: str,
        school: School,
        subreddit: str,
        feed_name: str,
        observed_at: datetime,
        cutoff: datetime,
    ) -> list[SourceItem]:
        path, params = JSON_FEEDS[feed_name]
        items: list[SourceItem] = []
        after: str | None = None
        for _page in range(self.config.max_pages):
            query = {"limit": "100", **params}
            if after:
                query["after"] = after
            url = f"{host}/r/{subreddit}/{path}?{urlencode(query)}"
            response = self.http.get(url, headers={"Accept": "application/json"})
            payload = response.json()
            children = payload["data"]["children"]
            page_items, oldest_on_page = self._normalize_json(
                children, school, subreddit, observed_at, cutoff
            )
            items.extend(page_items)
            after = payload["data"].get("after")
            if not after or not children:
                break
            if oldest_on_page is not None and oldest_on_page < cutoff:
                break
        return items

    def _normalize_json(
        self,
        children: list[dict[str, Any]],
        school: School,
        subreddit: str,
        observed_at: datetime,
        cutoff: datetime,
    ) -> tuple[list[SourceItem], datetime | None]:
        items: list[SourceItem] = []
        oldest: datetime | None = None
        for child in children:
            post = child.get("data") or {}
            post_id = str(post.get("id") or "")
            if not post_id:
                continue
            created = post.get("created_utc")
            published_at = (
                datetime.fromtimestamp(float(created), tz=UTC) if created is not None else None
            )
            if published_at is not None:
                oldest = published_at if oldest is None else min(oldest, published_at)
                if published_at < cutoff:
                    continue
            title = str(post.get("title") or "Reddit post")
            body = str(post.get("selftext") or "")
            permalink = str(post.get("permalink") or "")
            source_url = (
                f"https://www.reddit.com{permalink}"
                if permalink.startswith("/")
                else permalink or f"https://www.reddit.com/r/{subreddit}/"
            )
            external_id = f"t3_{post_id}"
            items.append(
                SourceItem(
                    source="reddit",
                    source_category=SourceCategory.FORUM,
                    external_id=external_id,
                    source_url=HttpUrl(source_url),
                    observed_at=observed_at,
                    published_at=published_at,
                    university_name=school.name,
                    title=title,
                    author=str(post.get("author") or "[unknown]"),
                    raw_text=body,
                    metadata={
                        "kind": "post",
                        "subreddit": subreddit,
                        "feed": "json",
                        "score": post.get("score"),
                    },
                    content_hash=content_hash(external_id, title, body),
                )
            )
        return items, oldest

    def _fetch_rss(
        self,
        host: str,
        school: School,
        subreddit: str,
        feed_name: str,
        observed_at: datetime,
        cutoff: datetime,
    ) -> list[SourceItem]:
        path = RSS_FEEDS[feed_name].format(subreddit=subreddit)
        feed_url = f"{host}{path}"
        xml = self.http.get(feed_url).text
        root = ET.fromstring(xml)
        entries = root.findall(f"{ATOM_NS}entry") or root.findall("item")
        result: list[SourceItem] = []
        for entry in entries:
            title = xml_text(entry, "title") or "Reddit post"
            link = xml_link(entry)
            published_at = parse_datetime(
                xml_text(entry, "updated")
                or xml_text(entry, "published")
                or xml_text(entry, "pubDate")
            )
            if published_at is not None and published_at < cutoff:
                continue
            html = (
                xml_text(entry, "content")
                or xml_text(entry, "summary")
                or xml_text(entry, "description")
            )
            body = BeautifulSoup(html, "html.parser").get_text("\n", strip=True) if html else ""
            author = xml_text(entry, "name") or xml_text(entry, "author") or "[unknown]"
            author = author.removeprefix("/u/")
            parsed = urlparse(link)
            match = REDDIT_POST_ID.search(parsed.path or link)
            external_id = f"t3_{match.group(1)}" if match else content_hash(link or title)
            source_url = link or f"https://www.reddit.com/r/{subreddit}/"
            result.append(
                SourceItem(
                    source="reddit",
                    source_category=SourceCategory.FORUM,
                    external_id=external_id,
                    source_url=HttpUrl(source_url),
                    observed_at=observed_at,
                    published_at=published_at,
                    university_name=school.name,
                    title=title,
                    author=author,
                    raw_text=body,
                    metadata={"kind": "post", "subreddit": subreddit, "feed": "rss"},
                    content_hash=content_hash(external_id, title, body),
                )
            )
        return result
