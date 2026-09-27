from __future__ import annotations

import re
from collections.abc import Callable

import httpx

from agentsenate.config import LegislatureConfig
from agentsenate.models import SourceCategory
from agentsenate.sources.listings import DocumentListingSource

HIGHER_ED = re.compile(
    r"\b(?:higher education|postsecondary|university|college|tuition|"
    r"student(?:s)?|campus|scholarship|hope|board of regents|"
    r"board of governors|board of trustees|financial aid|in-state)\b",
    re.IGNORECASE,
)


def _keep(title: str, text: str) -> bool:
    return bool(HIGHER_ED.search(f"{title}\n{text}"))


class LegislatureSource(DocumentListingSource):
    """State higher-education bills and committee packets."""

    def __init__(
        self,
        config: LegislatureConfig,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        targets = [(target.jurisdiction or target.name, target) for target in config.targets]
        super().__init__(
            config,
            targets,
            source="legislature",
            category=SourceCategory.STATE_LEGISLATURE,
            keep=_keep,
            client=client,
            sleeper=sleeper,
        )
