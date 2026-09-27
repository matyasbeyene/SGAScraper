from __future__ import annotations

import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import HttpUrl
from pypdf import PdfReader

from agentsenate.config import ListingConfig
from agentsenate.models import NamedTarget, SourceCategory, SourceItem
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

DOCUMENT_HREF = re.compile(
    r"(?:\.pdf(?:$|\?)|/bill/|/text/|legislation|resolution|minutes|agenda|"
    r"ordinance|senate-resources|wp-content/uploads|/article/|/news/|"
    r"/stories/|/feed)",
    re.IGNORECASE,
)
SKIP_HREF = re.compile(
    r"login|wp-admin|facebook|twitter|instagram|linkedin|mailto:|javascript:",
    re.IGNORECASE,
)
YEAR_PATH = re.compile(r"/(20\d{2})/(\d{1,2})/")


class DocumentListingSource:
    """RSS or HTML listings of bills, minutes, agendas, and news articles."""

    def __init__(
        self,
        config: ListingConfig,
        targets: Sequence[tuple[str | None, NamedTarget]],
        *,
        source: str,
        category: SourceCategory,
        keep: Callable[[str, str], bool],
        skip: Callable[[str, str], bool] | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.targets = targets
        self.source = source
        self.category = category
        self.keep = keep
        self.skip = skip
        http_client = client or httpx.Client(
            timeout=45,
            follow_redirects=True,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/rss+xml, text/html, application/pdf, */*",
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
        for school_name, target in self.targets:
            try:
                items.extend(self._fetch_target(school_name, target, now, cutoff))
            except httpx.HTTPError as exc:
                logger.warning("%s %s failed: %s", self.source, target.url, exc)
        unique: dict[str, SourceItem] = {}
        for item in items:
            unique.setdefault(item.external_id, item)
        if not unique:
            logger.info("No %s items kept", self.source)
        return list(unique.values())

    def _fetch_target(
        self,
        school_name: str | None,
        target: NamedTarget,
        observed_at: datetime,
        cutoff: datetime,
    ) -> list[SourceItem]:
        url = str(target.url)
        response = self.http.get(url)
        body = response.text
        if looks_like_feed(response.headers.get("content-type", ""), body):
            entries = _parse_feed(body)
        else:
            entries = _parse_listing(url, body)
        budget = min(len(entries), self.config.max_items_per_target * 2)
        items: list[SourceItem] = []
        for entry in entries[:budget]:
            if len(items) >= self.config.max_items_per_target:
                break
            published_at = entry.get("published_at")
            if isinstance(published_at, datetime) and published_at < cutoff:
                continue
            item_url = str(entry["url"])
            title = str(entry["title"])
            text = str(entry.get("text") or "")
            if self.config.fetch_body and len(text) < 400:
                try:
                    page = self._read_document(item_url)
                except (httpx.HTTPError, ValueError) as exc:
                    logger.info("%s document %s failed: %s", self.source, item_url, exc)
                    page = {}
                title = str(page.get("title") or title)
                text = str(page.get("text") or text)
                published_at = page.get("published_at") or published_at
            if published_at is None:
                published_at = _date_from_url(item_url)
            if isinstance(published_at, datetime) and published_at < cutoff:
                continue
            if self.skip and self.skip(title, text):
                continue
            if not self.keep(title, text):
                continue
            items.append(
                SourceItem(
                    source=self.source,
                    source_category=self.category,
                    external_id=content_hash(item_url),
                    source_url=HttpUrl(item_url),
                    observed_at=observed_at,
                    published_at=published_at if isinstance(published_at, datetime) else None,
                    university_name=school_name or target.jurisdiction,
                    title=title,
                    raw_text=text[:20_000],
                    metadata={
                        "feed": target.name,
                        "listing_url": url,
                        "jurisdiction": target.jurisdiction,
                    },
                    content_hash=content_hash(item_url, title, text[:2_000]),
                )
            )
        return items

    def _read_document(self, url: str) -> dict[str, Any]:
        response = self.http.get(url)
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or url.lower().split("?", 1)[0].endswith(".pdf"):
            reader = PdfReader(io.BytesIO(response.content))
            pages = [page.extract_text() or "" for page in reader.pages[:8]]
            text = "\n".join(pages)
            title = (pages[0].split("\n", 1)[0] if pages else "")[:180]
            return {"title": title.strip(), "text": text[:20_000], "published_at": None}
        soup = BeautifulSoup(response.text, "html.parser")
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
                if isinstance(block, dict):
                    published = published or parse_datetime(str(block.get("datePublished") or ""))
                    title = title or str(block.get("headline") or "")
        for junk in soup(["script", "style", "nav", "footer", "header", "form"]):
            junk.decompose()
        article = soup.find("article") or soup.find("main") or soup.body
        text = article.get_text("\n", strip=True) if article else ""
        return {"title": title, "text": text[:20_000], "published_at": published}


def _parse_feed(xml: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml)
    nodes = (
        root.findall(f"{ATOM_NS}entry") or root.findall("item") or root.findall("./channel/item")
    )
    entries: list[dict[str, Any]] = []
    for node in nodes:
        title = xml_text(node, "title") or "Untitled"
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
            {"title": title, "url": link, "text": text, "published_at": published_at}
        )
    return entries


def _parse_listing(listing_url: str, html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(listing_url).netloc
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = urljoin(listing_url, str(anchor["href"]))
        parsed = urlparse(href)
        if href in seen or SKIP_HREF.search(href):
            continue
        if parsed.netloc and parsed.netloc != host and not href.lower().endswith(".pdf"):
            continue
        title = anchor.get_text(" ", strip=True) or href.rsplit("/", 1)[-1]
        if not DOCUMENT_HREF.search(href) and not DOCUMENT_HREF.search(title):
            continue
        seen.add(href)
        entries.append(
            {
                "title": title,
                "url": href,
                "text": "",
                "published_at": _date_from_url(href) or _date_from_text(title),
            }
        )
    return entries


def _date_from_url(url: str) -> datetime | None:
    match = YEAR_PATH.search(urlparse(url).path)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=UTC)
    except ValueError:
        return None


def _date_from_text(text: str) -> datetime | None:
    return parse_datetime(text)


def _rss_guid(node: ET.Element) -> str:
    guid = node.find("guid")
    return (guid.text or "").strip() if guid is not None else ""
