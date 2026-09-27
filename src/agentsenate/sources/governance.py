from __future__ import annotations

from collections.abc import Callable

import httpx

from agentsenate.config import ListingConfig
from agentsenate.models import School, SourceCategory
from agentsenate.sources.common import is_policy_signal
from agentsenate.sources.listings import DocumentListingSource


class GovernanceSource(DocumentListingSource):
    """Faculty senate, university council, and system-board packets."""

    def __init__(
        self,
        config: ListingConfig,
        schools: list[School],
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        targets = [(school.name, target) for school in schools for target in school.governance]
        super().__init__(
            config,
            targets,
            source="governance",
            category=SourceCategory.GOVERNANCE,
            keep=is_policy_signal,
            client=client,
            sleeper=sleeper,
        )
