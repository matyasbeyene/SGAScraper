from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentsenate.models import School


class RedditConfig(BaseModel):
    enabled: bool = True
    include_top_level_comments: bool = False
    min_request_interval_seconds: float = Field(default=61.0, ge=0)
    lookback_days: int = Field(default=90, ge=1, le=365)
    feeds: list[str] = Field(default_factory=lambda: ["new", "hot", "top_week", "top_month"])
    max_pages: int = Field(default=2, ge=1, le=10)


class USGConfig(BaseModel):
    enabled: bool = True
    archive_url: str = "https://www.usg.edu/regents/meetings/date/{year}/"
    min_request_interval_seconds: float = Field(default=0.5, ge=0)
    document_kinds: list[str] = Field(
        default_factory=lambda: ["agenda", "minutes", "notice", "committee"]
    )


class NewsletterConfig(BaseModel):
    enabled: bool = True
    min_request_interval_seconds: float = Field(default=1.0, ge=0)
    max_articles_per_feed: int = Field(default=25, ge=1, le=100)
    fetch_article_body: bool = True


class YikYakConfig(BaseModel):
    enabled: bool = False
    provider: str | None = None


class SourcesConfig(BaseModel):
    reddit: RedditConfig = Field(default_factory=RedditConfig)
    usg_board: USGConfig = Field(default_factory=USGConfig)
    newsletters: NewsletterConfig = Field(default_factory=NewsletterConfig)
    yikyak: YikYakConfig = Field(default_factory=YikYakConfig)


class EmailConfig(BaseModel):
    recipients: list[str] = Field(default_factory=list)
    reply_to: str = ""
    authorized_reply_senders: list[str] = Field(default_factory=list)
    max_topics: int = Field(default=8, ge=1, le=25)
    minimum_actionability_score: int = Field(default=3, ge=1, le=5)
    maximum_attachment_bytes: int = Field(default=5_000_000, ge=1)
    maximum_total_attachment_bytes: int = Field(default=10_000_000, ge=1)


class FileConfig(BaseModel):
    schools: list[School] = Field(default_factory=list)
    email: EmailConfig = Field(default_factory=EmailConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    storage_backend: str = "supabase"
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "agentsenate/0.1"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_research_model: str = "claude-sonnet-4-6"
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    turso_database_url: str = ""
    turso_auth_token: str = ""
    resend_api_key: str = ""
    resend_from: str = ""
    resend_webhook_secret: str = ""
    gmail_address: str = ""
    gmail_app_password: str = ""
    agentsenate_config: Path = Path("config/schools.yaml")
    log_level: str = "INFO"


def load_file_config(path: Path) -> FileConfig:
    with path.open(encoding="utf-8") as handle:
        data: dict[str, Any] = yaml.safe_load(handle) or {}
    return FileConfig.model_validate(data)
