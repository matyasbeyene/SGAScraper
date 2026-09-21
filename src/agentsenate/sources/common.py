from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol
from xml.etree.ElementTree import Element

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from agentsenate.models import SourceItem

ATOM_NS = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
DEFAULT_USER_AGENT = "agentsenate/0.1 (+campus source monitor)"


def content_hash(*parts: str) -> str:
    normalized = "\n".join(part.strip() for part in parts)
    return hashlib.sha256(normalized.encode()).hexdigest()


class SourceAdapter(Protocol):
    def fetch(self, now: datetime, lookback: timedelta) -> list[SourceItem]: ...


def is_retryable_http(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {429, 503}


class RateLimitedClient:
    def __init__(
        self,
        client: httpx.Client,
        interval_seconds: float,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.interval_seconds = interval_seconds
        self.sleeper = sleeper
        self._last_request_at: float | None = None

    def _respect_rate_limit(self) -> None:
        if self.interval_seconds <= 0 or self._last_request_at is None:
            self._last_request_at = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.interval_seconds:
            self.sleeper(self.interval_seconds - elapsed)
        self._last_request_at = time.monotonic()

    @retry(
        retry=retry_if_exception(is_retryable_http),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=70),
        reraise=True,
    )
    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self._respect_rate_limit()
        response = self.client.get(url, **kwargs)  # type: ignore[arg-type]
        if response.status_code == 429:
            retry_after = float(response.headers.get("retry-after", "60"))
            self.sleeper(min(retry_after, 90))
        response.raise_for_status()
        return response


def xml_text(node: Element, local_name: str) -> str:
    for prefix in (CONTENT_NS, ATOM_NS, DC_NS, ""):
        found = node.find(f".//{prefix}{local_name}")
        if found is not None:
            text = "".join(found.itertext()).strip()
            if text:
                return text
    return ""


def xml_link(node: Element) -> str:
    link = node.find(f"{ATOM_NS}link")
    if link is not None:
        href = (link.get("href") or link.text or "").strip()
        if href:
            return href
    link = node.find("link")
    if link is not None:
        return (link.get("href") or link.text or "").strip()
    ident = node.find(f"{ATOM_NS}id")
    return (ident.text or "").strip() if ident is not None else ""


def parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def looks_like_feed(content_type: str, body: str) -> bool:
    lowered = content_type.lower()
    stripped = body.lstrip()
    xmlish = stripped.startswith(("<?xml", "<rss", "<feed"))
    if "rss" in lowered or "atom" in lowered or "xml" in lowered:
        return xmlish
    return xmlish
