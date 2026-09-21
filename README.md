# AgentSenate Daily Initiative Monitor

A scheduled Python service that watches school Reddit communities and University System of
Georgia Board of Regents agendas/minutes, asks Claude to identify actionable initiatives, and
sends a ranked daily digest through Resend. Turso keeps durable item and run history.

Yik Yak is intentionally disabled: it has no supported public developer API. The adapter fails
closed if enabled without a future approved provider implementation.

## How it works

1. At 8:00 AM Eastern, GitHub Actions fetches Reddit posts from public RSS feeds (`new`, `hot`,
   `top/week`, and `top/month`) for the last 90 days, plus current and prior USG meeting archives.
2. Turso inserts previously unseen source IDs. The first successful production run establishes
   a baseline and sends nothing, preventing an archive flood.
3. Claude screens all unscreened items, returns validated structured analysis, and ranks concrete
   campus-facing ideas. Anonymous posts are explicitly presented as unverified signals.
4. If any item meets the configured threshold, Resend delivers HTML and plain-text versions.
   Empty digests are skipped.
5. Authorized recipients can reply with text or screenshots. A signed Resend webhook sends the
   submission through Claude vision and web search, then returns a sourced brief in the same
   email thread.

## Setup

Requirements: Python 3.12, an Anthropic API key, a Turso account, and a Gmail app password or
verified Resend sending domain. Reddit uses public RSS feeds, so no Reddit API app is needed.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Create and initialize a Turso database:

```bash
turso auth login
turso db create agentsenate
turso db shell agentsenate < db/schema.sql
turso db show --url agentsenate
turso db tokens create agentsenate
```

Put the resulting URL and token in `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`. Keep the token only
in `.env` or GitHub Secrets.

Reddit monitoring uses public feeds like `https://www.reddit.com/r/UGA/new/.rss`. Add school
subreddit names in [`config/schools.yaml`](config/schools.yaml); no Reddit login is required.
Requests are spaced about one minute apart to stay within Reddit's RSS rate limit. Top-level
comments are not available through RSS.

Add schools and recipients in [`config/schools.yaml`](config/schools.yaml):

```yaml
schools:
  - name: University of Georgia
    aliases: [UGA, Georgia]
    subreddits: [UGA]

email:
  recipients:
    - student@example.edu
  reply_to: research@inbound.example.edu
  authorized_reply_senders: [] # defaults to recipients
  max_topics: 8
  minimum_actionability_score: 3
```

Set all values from [`.env.example`](.env.example). `RESEND_FROM` must use a verified Resend
identity. Run locally:

```bash
agentsenate --dry-run
agentsenate
```

Dry-run mode uses in-memory state, never sends email, and writes `digest-preview.html` when useful
topics exist. It still makes live Reddit, USG, and Anthropic requests.

## GitHub Actions

Add these repository secrets:

- `ANTHROPIC_API_KEY`
- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`

Reddit no longer needs GitHub secrets. Add repository variable `ANTHROPIC_MODEL` if you want to
override the default (`claude-sonnet-4-5`).
The workflow has two UTC schedules and an Eastern-time guard so daylight-saving changes do not
shift the local delivery time. Manual runs default to dry-run and upload the HTML preview.

## Reply research on Vercel

Apply the latest [`db/schema.sql`](db/schema.sql), including the `inbound_messages` table. In
Resend, enable receiving for the domain used by `email.reply_to`, then create an
`email.received` webhook pointing to:

```text
https://YOUR-VERCEL-PROJECT.vercel.app/api/resend_webhook
```

Deploy this repository to Vercel and configure:

- `ANTHROPIC_API_KEY` and `ANTHROPIC_RESEARCH_MODEL` (`claude-sonnet-4-6`)
- `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`
- `RESEND_API_KEY`, `RESEND_FROM`, and the webhook's `RESEND_WEBHOOK_SECRET`

The endpoint verifies Resend's Svix signature, accepts mail only from configured recipients (or
`authorized_reply_senders`), strips quoted history, and permits at most five PNG/JPEG/WebP images.
Default limits are 5 MB per image and 10 MB total. Webhook retries are deduplicated in Turso.
Submitted screenshots are treated as untrusted, anonymous signals; the response clearly separates
corroborated facts from unknown claims and includes source links.

## Development

```bash
ruff check .
ruff format --check .
mypy
pytest --cov=agentsenate
```

Tests use local fixtures and fakes; they do not contact or charge external services.
