from __future__ import annotations

import logging
from datetime import datetime, timedelta

from agentsenate.config import YikYakConfig
from agentsenate.models import SourceItem

logger = logging.getLogger(__name__)


class YikYakSource:
    def __init__(self, config: YikYakConfig) -> None:
        self.config = config

    def fetch(self, now: datetime, lookback: timedelta) -> list[SourceItem]:
        del now, lookback
        if self.config.enabled:
            raise RuntimeError(
                "Yik Yak has no supported public API. Configure an approved provider adapter first."
            )
        logger.info("Yik Yak adapter disabled (no supported public API/provider configured)")
        return []
