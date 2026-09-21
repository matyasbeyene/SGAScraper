from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator


class SourceCategory(StrEnum):
    FORUM = "Forum"
    BOARD_OF_REGENTS = "Board of Regents"
    NEWSLETTER = "Newsletter"


class SourceItem(BaseModel):
    source: str
    source_category: SourceCategory
    external_id: str
    source_url: HttpUrl
    observed_at: datetime
    published_at: datetime | None = None
    university_name: str | None = None
    title: str
    author: str | None = None
    raw_text: str
    document_id: str | None = None
    policy_status: str | None = None
    financial_cost: float | None = None
    funding_source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str


class InitiativeAnalysis(BaseModel):
    external_id: str
    is_useful: bool
    topic_tags: list[str] = Field(default_factory=list)
    impact_classification: str
    target_stakeholder: str
    executive_summary: str
    actionability_score: int = Field(ge=1, le=5)
    trend_alert_flag: bool = False
    evidence: str
    ranking_rationale: str

    @field_validator("actionability_score", mode="before")
    @classmethod
    def clamp_actionability_score(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return 1
        try:
            score = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, min(5, score))


class AnalysisBatch(BaseModel):
    initiatives: list[InitiativeAnalysis]


class NewsletterTarget(BaseModel):
    name: str
    url: HttpUrl


class School(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    subreddits: list[str] = Field(default_factory=list)
    newsletters: list[NewsletterTarget] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchCitation(BaseModel):
    title: str
    url: HttpUrl
    supports: str


class ResearchResult(BaseModel):
    subject: str
    identified_initiative: str
    assessment: str
    verification_status: str
    comparable_policies: list[str] = Field(default_factory=list)
    stakeholders: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    citations: list[ResearchCitation] = Field(default_factory=list)


class InboundImage(BaseModel):
    filename: str
    media_type: str
    content: bytes
