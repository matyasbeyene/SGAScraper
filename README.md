# AgentSenate Daily Initiative Monitor

A scheduled Python service that watches school Reddit communities, campus newsletters, and
University System of Georgia Board of Regents agendas/minutes, asks DeepSeek to identify
actionable student-government initiatives, and sends a ranked daily digest by email. Supabase
keeps durable item and run history.

Yik Yak is intentionally disabled: it has no supported public developer API. The adapter fails
closed if enabled without a future approved provider implementation.

## How it works

1. At 8:00 AM Eastern, GitHub Actions fetches Reddit posts from configured public subreddit
   listings, campus newsletters, and current/prior USG meeting archives.
2. Supabase inserts previously unseen source IDs. The first successful production run establishes
   a baseline and sends nothing, preventing an archive flood. Use `--send-initial-digest` to
   explicitly include the first collection in a digest.
3. DeepSeek screens all unscreened items, returns validated structured analysis, and ranks
   concrete campus-facing ideas. Anonymous posts are explicitly presented as unverified signals.
4. If any item meets the configured threshold, Gmail SMTP or Resend delivers HTML and plain-text
   versions. Empty digests are skipped.
5. A Resend inbound webhook can be enabled later for reply-based research; the daily outbound MVP
   does not require it.

## Setup

Requirements: Python 3.12, a DeepSeek API key, the Supabase project, and a Gmail app password or
verified Resend sending domain. Reddit uses public listings, so no Reddit API app is needed for
the current MVP.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Create and initialize the Supabase schema:

1. Open the Supabase SQL editor for the `SGA Scraper` project.
2. Run [`db/schema.supabase.sql`](db/schema.supabase.sql).
3. Put the project URL and service-role key in `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.

Keep the service-role key only in `.env` or GitHub Secrets. It bypasses RLS and must never be
committed or exposed to browser code.

Reddit monitoring uses public subreddit listings like `https://www.reddit.com/r/UGA/new.json`.
Add school subreddit names in [`config/schools.yaml`](config/schools.yaml); no Reddit login is
required for this MVP. Top-level comments are not collected.

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
identity if you choose Resend. For the simplest free email path, set `GMAIL_ADDRESS` and
`GMAIL_APP_PASSWORD`. Run locally:

```bash
agentsenate --dry-run
agentsenate
```

Dry-run mode uses in-memory state, never sends email, and writes `digest-preview.html` when useful
topics exist. It still makes live Reddit, USG, newsletter, and DeepSeek requests.

## GitHub Actions

Add these repository secrets:

- `DEEPSEEK_API_KEY`
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`

Reddit no longer needs GitHub secrets. The workflow has two UTC schedules and an Eastern-time
guard so daylight-saving changes do not shift the local delivery time. Pushes run a dry-run and
upload a UGA-only HTML preview. Scheduled and manual runs use all 24 SEC and Ivy League schools.
Manual runs also dry-run unless `send_email` is set to `true`; select `send_initial_digest` to
send the first collection immediately. Later runs only consider newly collected items.

The legacy reply-research workflow stays disabled unless the repository variable
`ENABLE_REPLY_RESEARCH` is `true`. It still requires an Anthropic key; the DeepSeek daily digest
does not. Email replies are not processed by the outbound monitor.

## Reply research on Vercel

Apply the latest [`db/schema.supabase.sql`](db/schema.supabase.sql), including the
`inbound_messages` table. In
Resend, enable receiving for the domain used by `email.reply_to`, then create an
`email.received` webhook pointing to:

```text
https://YOUR-VERCEL-PROJECT.vercel.app/api/resend_webhook
```

Deploy this repository to Vercel and configure:

- `ANTHROPIC_API_KEY` and `ANTHROPIC_RESEARCH_MODEL` (`claude-sonnet-4-6`)
- `STORAGE_BACKEND`, `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY`
- `RESEND_API_KEY`, `RESEND_FROM`, and the webhook's `RESEND_WEBHOOK_SECRET`

The endpoint verifies Resend's Svix signature, accepts mail only from configured recipients (or
`authorized_reply_senders`), strips quoted history, and permits at most five PNG/JPEG/WebP images.
Default limits are 5 MB per image and 10 MB total. Webhook retries are deduplicated in storage.
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
