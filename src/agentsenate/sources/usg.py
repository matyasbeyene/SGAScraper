from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl
from pypdf import PdfReader

from agentsenate.config import USGConfig
from agentsenate.models import SourceCategory, SourceItem
from agentsenate.sources.common import DEFAULT_USER_AGENT, RateLimitedClient, content_hash

logger = logging.getLogger(__name__)

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
KIND_PATTERNS = (
    ("minutes", re.compile(r"minutes", re.I)),
    ("agenda", re.compile(r"agenda", re.I)),
    ("committee", re.compile(r"committee|executive|compensation", re.I)),
    ("notice", re.compile(r"notice", re.I)),
)


class USGBoardSource:
    def __init__(self, config: USGConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self.http = RateLimitedClient(
            client
            or httpx.Client(
                timeout=60,
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            ),
            config.min_request_interval_seconds,
        )

    def fetch(self, now: datetime, lookback: timedelta) -> list[SourceItem]:
        if not self.config.enabled:
            return []
        cutoff = now - lookback
        items: list[SourceItem] = []
        seen: set[str] = set()
        for year in (now.year - 1, now.year):
            archive_url = self.config.archive_url.format(year=year)
            try:
                soup = BeautifulSoup(self.http.get(archive_url).text, "html.parser")
            except httpx.HTTPError as exc:
                logger.warning("USG archive %s failed: %s", archive_url, exc)
                continue
            for anchor in soup.find_all("a", href=True):
                document_url = urljoin(archive_url, str(anchor["href"]))
                if document_url in seen or not document_url.lower().endswith(".pdf"):
                    continue
                row = anchor.find_parent("tr")
                if row is None:
                    continue
                kind = _document_kind(anchor.get_text(" ", strip=True), document_url)
                if kind is None or kind not in self.config.document_kinds:
                    continue
                seen.add(document_url)
                first_cell = row.find("td") if row is not None else None
                meeting = (
                    first_cell.get_text(" ", strip=True)
                    if first_cell is not None
                    else f"{year} Board meeting"
                )
                published_at = parse_meeting_date(meeting) or parse_meeting_date(document_url)
                if published_at is not None and published_at < cutoff:
                    continue
                try:
                    raw_text = self._extract_document(document_url)
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("Could not extract USG document %s: %s", document_url, exc)
                    raw_text = ""
                external_id = hashlib.sha256(document_url.encode()).hexdigest()
                title = f"USG Board of Regents {kind.title()} — {meeting}"
                items.append(
                    SourceItem(
                        source="usg_board",
                        source_category=SourceCategory.BOARD_OF_REGENTS,
                        external_id=external_id,
                        source_url=HttpUrl(document_url),
                        observed_at=now,
                        published_at=published_at,
                        title=title,
                        raw_text=raw_text,
                        document_id=f"USG-{year}-{kind}-{external_id[:10]}",
                        metadata={
                            "document_type": kind,
                            "meeting": meeting,
                            "year": year,
                        },
                        content_hash=content_hash(document_url, raw_text),
                    )
                )
        return items

    def _extract_document(self, url: str) -> str:
        response = self.http.get(url)
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            reader = PdfReader(io.BytesIO(response.content))
            return "\n".join(page.extract_text() or "" for page in reader.pages)[:250_000]
        soup = BeautifulSoup(response.text, "html.parser")
        return soup.get_text("\n", strip=True)[:250_000]


def _document_kind(link_text: str, url: str) -> str | None:
    for kind, pattern in KIND_PATTERNS:
        if pattern.search(url):
            return kind
    for kind, pattern in KIND_PATTERNS:
        if pattern.search(link_text):
            return kind
    return None


def parse_meeting_date(text: str) -> datetime | None:
    cleaned = re.sub(r"(\d{1,2})\s*[-–]\s*\d{1,2}", r"\1", text)
    cleaned = re.sub(r"%20", " ", cleaned)
    cleaned = re.sub(r"[-_,.]+", " ", cleaned)
    month_pattern = "|".join(MONTHS)
    match = re.search(
        rf"({month_pattern})\s+(\d{{1,2}})\s+(\d{{4}})",
        cleaned,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        parsed = datetime.strptime(
            f"{match.group(1)} {match.group(2)} {match.group(3)}",
            "%B %d %Y",
        )
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)
