from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentsenate.analyzer import ClaudeAnalyzer, DeepSeekAnalyzer
from agentsenate.config import FileConfig, Secrets, load_file_config
from agentsenate.emailer import ResendMailer
from agentsenate.gmail import GmailSender
from agentsenate.pipeline import Mailer, Pipeline
from agentsenate.sources import (
    NewsletterSource,
    RedditSource,
    SourceAdapter,
    USGBoardSource,
    YikYakSource,
)
from agentsenate.storage import MemoryStorage, SupabaseRestStorage, TursoStorage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the daily initiative monitor")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use in-memory state and write digest-preview.html instead of sending email",
    )
    parser.add_argument(
        "--send-initial-digest",
        action="store_true",
        help="Analyze and email the first collection instead of only creating a baseline",
    )
    parser.add_argument(
        "--preview",
        type=Path,
        default=Path("digest-preview.html"),
        help="HTML output path used with --dry-run",
    )
    parser.add_argument(
        "--sources-only",
        action="store_true",
        help="Fetch scrapers only; skip Claude and email",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("scraped-items.json"),
        help="JSON path used with --sources-only",
    )
    return parser


def build_sources(config: FileConfig, user_agent: str) -> dict[str, SourceAdapter]:
    return {
        "reddit": RedditSource(
            config.sources.reddit,
            config.schools,
            user_agent,
        ),
        "usg_board": USGBoardSource(config.sources.usg_board),
        "newsletters": NewsletterSource(config.sources.newsletters, config.schools),
        "yikyak": YikYakSource(config.sources.yikyak),
    }


def main() -> None:
    args = build_parser().parse_args()
    secrets = Secrets()
    logging.basicConfig(
        level=getattr(logging, secrets.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_file_config(secrets.agentsenate_config)
    lookback = timedelta(days=config.sources.reddit.lookback_days)
    sources = build_sources(config, secrets.reddit_user_agent)
    if args.sources_only:
        now = datetime.now(UTC)
        payload: dict[str, list[dict[str, object]]] = {}
        for name, source in sources.items():
            items = source.fetch(now, lookback)
            payload[name] = [item.model_dump(mode="json") for item in items]
            logging.getLogger("agentsenate.cli").info("Fetched %s %s items", len(items), name)
        args.out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(
            json.dumps(
                {name: len(rows) for name, rows in payload.items()} | {"out": str(args.out)},
                indent=2,
            )
        )
        return
    storage = MemoryStorage(previously_ran=True) if args.dry_run else _build_storage(secrets)
    analyzer = _build_analyzer(secrets, config.email.max_topics)
    mailer: Mailer | None
    if args.dry_run:
        mailer = None
    elif secrets.gmail_address and secrets.gmail_app_password:
        mailer = GmailSender(
            secrets.gmail_address,
            secrets.gmail_app_password,
            config.email.recipients,
            config.email.reply_to,
        )
    else:
        mailer = ResendMailer(
            secrets.resend_api_key,
            secrets.resend_from,
            config.email.recipients,
            config.email.reply_to,
        )
    preview_writer = (
        (lambda html: args.preview.write_text(html, encoding="utf-8")) if args.dry_run else None
    )
    result = Pipeline(
        sources=sources,
        storage=storage,
        analyzer=analyzer,
        mailer=mailer,
        minimum_score=config.email.minimum_actionability_score,
        maximum_topics=config.email.max_topics,
        lookback=lookback,
        preview_writer=preview_writer,
        send_initial_digest=args.send_initial_digest,
    ).run()
    print(json.dumps(result, indent=2, default=str))


def _build_storage(secrets: Secrets) -> TursoStorage | SupabaseRestStorage:
    if secrets.storage_backend == "turso":
        return TursoStorage(secrets.turso_database_url, secrets.turso_auth_token)
    if secrets.storage_backend == "supabase":
        return SupabaseRestStorage(secrets.supabase_url, secrets.supabase_service_role_key)
    raise RuntimeError("STORAGE_BACKEND must be 'supabase' or 'turso'")


def _build_analyzer(secrets: Secrets, max_topics: int) -> ClaudeAnalyzer | DeepSeekAnalyzer:
    if secrets.deepseek_api_key:
        return DeepSeekAnalyzer(
            api_key=secrets.deepseek_api_key,
            model=secrets.deepseek_model,
            base_url=secrets.deepseek_base_url,
            max_topics=max_topics,
        )
    return ClaudeAnalyzer(
        secrets.anthropic_api_key,
        secrets.anthropic_model,
        max_topics=max_topics,
    )


if __name__ == "__main__":
    main()
