from __future__ import annotations

import io
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import HttpUrl
from pypdf import PdfReader

from agentsenate.config import YikYakConfig
from agentsenate.models import School, SourceCategory, SourceItem
from agentsenate.sources.common import content_hash

logger = logging.getLogger(__name__)

LOCAL_PROVIDER = "local_pdfs"


class YikYakSource:
    """Ingest Yik Yak posts that were exported as PDFs into a local drop folder."""

    def __init__(self, config: YikYakConfig, schools: list[School] | None = None) -> None:
        self.config = config
        self.schools = schools or []

    def fetch(self, now: datetime, lookback: timedelta) -> list[SourceItem]:
        if not self.config.enabled:
            logger.info("Yik Yak adapter disabled (drop exported PDFs in data/yikyak to enable)")
            return []
        if self.config.provider != LOCAL_PROVIDER:
            raise RuntimeError(
                "Yik Yak has no public API. Set sources.yikyak.provider to local_pdfs and drop "
                "Share/Print PDFs from the app into the configured drop_dir."
            )
        drop_dir = Path(self.config.drop_dir)
        drop_dir.mkdir(parents=True, exist_ok=True)
        saved_dir = drop_dir / "saved"
        saved_dir.mkdir(parents=True, exist_ok=True)
        cutoff = now - lookback
        items: list[SourceItem] = []
        for path in sorted(drop_dir.glob("*.pdf")):
            published_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if published_at < cutoff:
                continue
            try:
                raw_text = _pdf_text(path)
            except Exception as exc:
                logger.warning("Could not read Yik Yak PDF %s: %s", path, exc)
                continue
            digest = content_hash(path.name, raw_text)
            archive = saved_dir / f"{digest[:16]}.pdf"
            if not archive.exists():
                shutil.copy2(path, archive)
            school = _match_school(path.stem, raw_text, self.schools)
            title = path.stem.replace("_", " ").replace("-", " ").strip() or "Yik Yak export"
            items.append(
                SourceItem(
                    source="yikyak",
                    source_category=SourceCategory.FORUM,
                    external_id=f"yikyak_{digest[:24]}",
                    source_url=HttpUrl(f"https://yikyak.com/exports/{digest[:24]}"),
                    observed_at=now,
                    published_at=published_at,
                    university_name=school.name if school else None,
                    title=title,
                    raw_text=raw_text,
                    document_id=archive.name,
                    metadata={
                        "kind": "exported_pdf",
                        "original_filename": path.name,
                        "saved_path": str(archive),
                    },
                    content_hash=digest,
                )
            )
        logger.info("Yik Yak ingested %s exported PDFs from %s", len(items), drop_dir)
        return items


def _pdf_text(path: Path) -> str:
    reader = PdfReader(io.BytesIO(path.read_bytes()))
    return "\n".join(page.extract_text() or "" for page in reader.pages)[:20_000]


def _match_school(filename: str, text: str, schools: list[School]) -> School | None:
    blob = f"{filename} {text}".casefold()
    for school in schools:
        needles = [school.name, *school.aliases]
        if any(needle.casefold() in blob for needle in needles if needle):
            return school
    return None
