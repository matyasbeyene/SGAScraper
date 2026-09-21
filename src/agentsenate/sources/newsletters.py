from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree.ElementTree import Element

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import HttpUrl

from agentsenate.config import NewsletterConfig
from agentsenate.models import School, SourceCategory, SourceItem
from agentsenate.sources.common import (
    ATOM_NS,
    DEFAULT_USER_AGENT,
    RateLimitedClient,
    content_hash,
    looks_like_feed,
    parse_datetime,
    xml_link,
    xml_text,
)

logger = logging.getLogger(__name__)

ARTICLE_PATH = re.compile(
    r"/(?:20\d{2}/\d{2}/[\w-]+/?$|stories/20\d{2}/\d{2}/[\w-]+/?$"
    r"|news/20\d{2}/\d{2}/[\w-]+(?:/index\.html)?$)",
    re.IGNORECASE,
)


class NewsletterSource:
    """Public campus newsletters and newsrooms: RSS when available, HTML listings otherwise."""

    def __init__(
        self,
        config: NewsletterConfig,
        schools: list[School],
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.schools = schools
        http_client = client or httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/rss+xml, text/html, */*",
            },
        )
        interval = config.min_request_interval_seconds
        if sleeper is None:
            self.http = RateLimitedClient(http_client, interval)
        else:
            self.http = RateLimitedClient(http_client, interval, sleeper)

    def fetch(self, now: datetime, lookback: timedelta) -> list[SourceItem]:
        if not self.config.enabled:
            return []
        cutoff = now - lookback
        items: list[SourceItem] = []
        for school in self.schools:
            for target in school.newsletters:
                try:
                    items.extend(
                        self._fetch_target(school, target.name, str(target.url), now, cutoff)
                    )
                except httpx.HTTPError as exc:
                    logger.warning("Newsletter %s (%s) failed: %s", target.name, target.url, exc)
        unique: dict[str, SourceItem] = {}
        for item in items:
            unique.setdefault(item.external_id, item)
        return list(unique.values())

    def _fetch_target(
        self,
        school: School,
        feed_name: str,
        url: str,
        observed_at: datetime,
        cutoff: datetime,
    ) -> list[SourceItem]:
        response = self.http.get(url)
        body = response.text
        if looks_like_feed(response.headers.get("content-type", ""), body):
            entries = self._parse_feed(body)
        else:
            entries = self._parse_listing(url, body)
        selected = entries[: self.config.max_articles_per_feed]
        items: list[SourceItem] = []
        for entry in selected:
            published_at = entry.get("published_at")
            if isinstance(published_at, datetime) and published_at < cutoff:
                continue
            article_url = str(entry["url"])
            title = str(entry["title"])
            text = str(entry.get("text") or "")
            author = entry.get("author")
            if self.config.fetch_article_body and len(text) < 400:
                try:
                    page = self._read_article(article_url)
                except httpx.HTTPError as exc:
                    logger.info("Newsletter article %s failed: %s", article_url, exc)
                    page = {}
                title = str(page.get("title") or title)
                text = str(page.get("text") or text)
                published_at = page.get("published_at") or published_at
                author = page.get("author") or author
            if isinstance(published_at, datetime) and published_at < cutoff:
                continue
            external_id = content_hash(article_url)
            items.append(
                SourceItem(
                    source="newsletter",
                    source_category=SourceCategory.NEWSLETTER,
                    external_id=external_id,
                    source_url=HttpUrl(article_url),
                    observed_at=observed_at,
                    published_at=published_at if isinstance(published_at, datetime) else None,
                    university_name=school.name,
                    title=title,
                    author=str(author) if author else None,
                    raw_text=text[:20_000],
                    metadata={"feed": feed_name, "listing_url": url},
                    content_hash=content_hash(article_url, title, text[:2_000]),
                )
            )
        return items

    def _parse_feed(self, xml: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml)
        nodes = (
            root.findall(f"{ATOM_NS}entry")
            or root.findall("item")
            or root.findall("./channel/item")
        )
        entries: list[dict[str, Any]] = []
        for node in nodes:
            title = xml_text(node, "title") or "Campus newsletter"
            link = xml_link(node) or _rss_guid(node)
            if not link:
                continue
            html = (
                xml_text(node, "encoded")
                or xml_text(node, "content")
                or xml_text(node, "summary")
                or xml_text(node, "description")
            )
            text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True) if html else ""
            published_at = parse_datetime(
                xml_text(node, "updated")
                or xml_text(node, "published")
                or xml_text(node, "pubDate")
                or xml_text(node, "date")
            )
            entries.append(
                {
                    "title": title,
                    "url": link,
                    "text": text,
                    "published_at": published_at,
                    "author": xml_text(node, "creator") or xml_text(node, "author") or None,
                }
            )
        return entries

    def _parse_listing(self, listing_url: str, html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        host = urlparse(listing_url).netloc
        seen: set[str] = set()
        entries: list[dict[str, Any]] = []
        for anchor in soup.find_all("a", href=True):
            if not isinstance(anchor, Tag):
                continue
            href = urljoin(listing_url, str(anchor["href"]))
            parsed = urlparse(href)
            if parsed.netloc != host or href in seen:
                continue
            if not ARTICLE_PATH.search(parsed.path):
                continue
            seen.add(href)
            title = anchor.get_text(" ", strip=True) or href.rsplit("/", 1)[-1]
            entries.append({"title": title, "url": href, "text": "", "published_at": None})
        return entries

    def _read_article(self, url: str) -> dict[str, Any]:
        soup = BeautifulSoup(self.http.get(url).text, "html.parser")
        title = ""
        og = soup.find("meta", property="og:title")
        if isinstance(og, Tag) and og.get("content"):
            title = str(og["content"]).strip()
        if not title and soup.title:
            title = soup.title.get_text(" ", strip=True)
        published = None
        time_tag = soup.find("time")
        if isinstance(time_tag, Tag):
            published = parse_datetime(
                str(time_tag.get("datetime") or time_tag.get_text(" ", strip=True))
            )
        if published is None:
            meta = soup.find("meta", attrs={"property": "article:published_time"})
            if isinstance(meta, Tag):
                published = parse_datetime(str(meta.get("content") or ""))
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.get_text() or "")
            except json.JSONDecodeError:
                continue
            blocks = data if isinstance(data, list) else [data]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                published = published or parse_datetime(str(block.get("datePublished") or ""))
                title = title or str(block.get("headline") or "")
        for junk in soup(["script", "style", "nav", "footer", "header", "form"]):
            junk.decompose()
        article = soup.find("article") or soup.find("main") or soup.body
        text = article.get_text("\n", strip=True) if article else ""
        return {"title": title, "text": text[:20_000], "published_at": published}


def _rss_guid(node: Element) -> str:
    guid = node.find("guid")
    return (guid.text or "").strip() if guid is not None else ""
