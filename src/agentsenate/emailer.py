from __future__ import annotations

from datetime import datetime
from html import escape

import resend

from agentsenate.models import InitiativeAnalysis, ResearchResult, SourceItem


def select_topics(
    analyses: list[InitiativeAnalysis], minimum_score: int, maximum: int
) -> list[InitiativeAnalysis]:
    useful = [
        item for item in analyses if item.is_useful and item.actionability_score >= minimum_score
    ]
    return sorted(
        useful,
        key=lambda item: (item.trend_alert_flag, item.actionability_score),
        reverse=True,
    )[:maximum]


def render_digest(
    selected: list[InitiativeAnalysis],
    source_items: list[SourceItem],
    generated_at: datetime,
) -> tuple[str, str, str]:
    by_id = {item.external_id: item for item in source_items}
    subject = f"Daily Initiative Monitor — {generated_at:%B %-d, %Y}"
    groups = [
        ("Emerging trends", [item for item in selected if item.trend_alert_flag]),
        (
            "Quick wins",
            [
                item
                for item in selected
                if not item.trend_alert_flag and item.actionability_score >= 4
            ],
        ),
        (
            "Strategic targets",
            [
                item
                for item in selected
                if not item.trend_alert_flag and item.actionability_score < 4
            ],
        ),
    ]

    html_parts = [
        '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#172033;'
        'max-width:720px;margin:auto">',
        f'<h1 style="font-size:22px">{escape(subject)}</h1>',
        "<p>Newly observed campus-policy signals from the last daily monitoring cycle.</p>",
    ]
    text_parts = [subject, "Newly observed campus-policy signals.", ""]
    for heading, analyses in groups:
        if not analyses:
            continue
        html_parts.append(f'<h2 style="font-size:18px">{escape(heading)}</h2>')
        text_parts.extend([heading, "-" * len(heading)])
        for analysis in analyses:
            source = by_id[analysis.external_id]
            school = source.university_name or "University System of Georgia"
            tags = ", ".join(analysis.topic_tags)
            html_parts.append(
                '<section style="border-left:4px solid #4169e1;padding:2px 0 8px 14px;'
                'margin:14px 0">'
                f'<h3 style="margin-bottom:4px">{escape(source.title)}</h3>'
                f'<p style="margin:4px 0"><strong>{escape(school)}</strong> · '
                f"Actionability {analysis.actionability_score}/5</p>"
                f"<p>{escape(analysis.executive_summary)}</p>"
                f"<p><strong>Why it matters:</strong> {escape(analysis.ranking_rationale)}<br>"
                f"<strong>Stakeholder:</strong> {escape(analysis.target_stakeholder)}<br>"
                f"<strong>Evidence:</strong> {escape(analysis.evidence)}"
                + (f"<br><strong>Topics:</strong> {escape(tags)}" if tags else "")
                + f'</p><p><a href="{escape(str(source.source_url))}">View source</a></p>'
                "</section>"
            )
            text_parts.extend(
                [
                    source.title,
                    f"{school} | Actionability {analysis.actionability_score}/5",
                    analysis.executive_summary,
                    f"Why it matters: {analysis.ranking_rationale}",
                    f"Stakeholder: {analysis.target_stakeholder}",
                    f"Evidence: {analysis.evidence}",
                    f"Source: {source.source_url}",
                    "",
                ]
            )
    html_parts.append(
        '<hr><p style="font-size:12px;color:#667085">Anonymous forum posts are signals, '
        "not verified facts. Review linked sources before acting.</p></body></html>"
    )
    text_parts.append("Anonymous forum posts are signals, not verified facts.")
    return subject, "".join(html_parts), "\n".join(text_parts)


class ResendMailer:
    def __init__(
        self, api_key: str, sender: str, recipients: list[str], reply_to: str = ""
    ) -> None:
        if not api_key or not sender:
            raise RuntimeError("Resend API key and verified sender are required")
        if not recipients:
            raise RuntimeError("At least one email recipient must be configured")
        resend.api_key = api_key
        self.sender = sender
        self.recipients = recipients
        self.reply_to = reply_to

    def send(self, subject: str, html: str, text: str, idempotency_key: str) -> str:
        params: resend.Emails.SendParams = {
            "from": self.sender,
            "to": self.recipients,
            "subject": subject,
            "html": html,
            "text": text,
        }
        if self.reply_to:
            params["reply_to"] = self.reply_to
        response = resend.Emails.send(params, {"idempotency_key": idempotency_key})
        return str(response["id"])


def render_research(result: ResearchResult) -> tuple[str, str]:
    def html_list(items: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"

    html = [
        '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#172033;'
        'max-width:720px;margin:auto">',
        f"<h1>{escape(result.subject)}</h1>",
        f"<p><strong>Initiative:</strong> {escape(result.identified_initiative)}</p>",
        f"<p><strong>Status:</strong> {escape(result.verification_status)}</p>",
        f"<p>{escape(result.assessment)}</p>",
    ]
    text = [
        result.subject,
        f"Initiative: {result.identified_initiative}",
        f"Status: {result.verification_status}",
        result.assessment,
    ]
    for heading, items in (
        ("Comparable policies", result.comparable_policies),
        ("Stakeholders", result.stakeholders),
        ("Recommended actions", result.recommended_actions),
        ("Unknowns", result.unknowns),
    ):
        if items:
            html.extend([f"<h2>{heading}</h2>", html_list(items)])
            text.extend(["", heading, *[f"- {item}" for item in items]])
    html.append("<h2>Sources</h2><ul>")
    text.extend(["", "Sources"])
    for citation in result.citations:
        html.append(
            f'<li><a href="{escape(str(citation.url))}">{escape(citation.title)}</a>: '
            f"{escape(citation.supports)}</li>"
        )
        text.append(f"- {citation.title}: {citation.url} — {citation.supports}")
    html.append(
        '</ul><hr><p style="font-size:12px;color:#667085">Anonymous screenshots are treated '
        "as unverified signals unless corroborated by linked sources.</p></body></html>"
    )
    return "".join(html), "\n".join(text)


class ResendResearchMailer:
    def __init__(self, api_key: str, sender: str) -> None:
        if not api_key or not sender:
            raise RuntimeError("Resend API key and verified sender are required")
        resend.api_key = api_key
        self.sender = sender

    def send_reply(
        self,
        result: ResearchResult,
        recipient: str,
        original_subject: str,
        message_id: str,
        references: str,
        idempotency_key: str,
    ) -> str:
        html, text = render_research(result)
        headers = {"In-Reply-To": message_id}
        headers["References"] = " ".join(part for part in (references, message_id) if part)
        response = resend.Emails.send(
            {
                "from": self.sender,
                "to": [recipient],
                "subject": (
                    original_subject
                    if original_subject.lower().startswith("re:")
                    else f"Re: {original_subject}"
                ),
                "html": html,
                "text": text,
                "headers": headers,
            },
            {"idempotency_key": idempotency_key},
        )
        return str(response["id"])
