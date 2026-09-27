from __future__ import annotations

import re
from collections.abc import Callable

import httpx

from agentsenate.config import ListingConfig
from agentsenate.models import School, SourceCategory
from agentsenate.sources.listings import DocumentListingSource

LOCAL_KEEP = re.compile(
    r"\b(?:transit|shuttle|bus(?:es)?|housing|zoning|parking|towing|safety|"
    r"lighting|sidewalk|landlord|tenant|noise|student|ordinance|overlay|"
    r"affordable|rental|campus)\b",
    re.IGNORECASE,
)


def _keep(title: str, text: str) -> bool:
    return bool(LOCAL_KEEP.search(f"{title}\n{text}"))


class LocalGovSource(DocumentListingSource):
    """City and county agendas that affect students near campus."""

    def __init__(
        self,
        config: ListingConfig,
        schools: list[School],
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        targets = [(school.name, target) for school in schools for target in school.local_gov]
        super().__init__(
            config,
            targets,
            source="local_gov",
            category=SourceCategory.LOCAL_GOVERNMENT,
            keep=_keep,
            client=client,
            sleeper=sleeper,
        )
