from __future__ import annotations

import re
from collections.abc import Callable

import httpx

from agentsenate.config import ListingConfig
from agentsenate.models import School, SourceCategory
from agentsenate.sources.common import CEREMONIAL_NOISE, is_policy_signal
from agentsenate.sources.listings import DocumentListingSource

BILLISH = re.compile(r"\b(?:bill|resolution|legislation|act)\b", re.IGNORECASE)


def _keep(title: str, text: str) -> bool:
    return is_policy_signal(title, text) or bool(BILLISH.search(title))


def _skip(title: str, text: str) -> bool:
    return bool(CEREMONIAL_NOISE.search(f"{title}\n{text}"))


class SGASource(DocumentListingSource):
    """Peer student-government bills, resolutions, and senate packets."""

    def __init__(
        self,
        config: ListingConfig,
        schools: list[School],
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        targets = [(school.name, target) for school in schools for target in school.sga]
        super().__init__(
            config,
            targets,
            source="sga",
            category=SourceCategory.SGA,
            keep=_keep,
            skip=_skip,
            client=client,
            sleeper=sleeper,
        )
