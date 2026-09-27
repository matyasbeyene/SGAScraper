from __future__ import annotations

from collections.abc import Callable

import httpx

from agentsenate.config import TradePressConfig
from agentsenate.models import SourceCategory
from agentsenate.sources.common import is_policy_signal, is_sports_noise
from agentsenate.sources.listings import DocumentListingSource


def _keep(title: str, text: str) -> bool:
    return is_policy_signal(title, text) and not is_sports_noise(title, text)


class TradePressSource(DocumentListingSource):
    """National higher-ed press: model policies and emerging fights."""

    def __init__(
        self,
        config: TradePressConfig,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        targets = [("National", target) for target in config.targets]
        super().__init__(
            config,
            targets,
            source="trade_press",
            category=SourceCategory.TRADE_PRESS,
            keep=_keep,
            client=client,
            sleeper=sleeper,
        )
